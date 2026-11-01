"""run_multi_symbol.py — Multi-symbol orchestrator for ApexQuant V2.

SAFETY GUARANTEES:
  1. state_dir must NOT be paper_state_dir (or inside it).
  2. Reconciler ERROR aborts the entire cycle before processing.
  3. All symbols are processed against one shared working state.
  4. Candles are processed in global chronological order.
  5. Same-timestamp candles use whitelist order as deterministic tie-break.
  6. Events are appended before state commit.
  7. State is saved once after the complete processing cycle.
  8. Catchup is capped at MAX_CATCHUP_CANDLES per symbol per cycle.
  9. V1 state is never touched.

IMPORTANT:
  Event append and state save are separate filesystem operations.
  This is not a fully atomic transaction. A crash after event append
  but before state save requires event deduplication and recovery checks.

Exit codes:
  0 = OK
  1 = FAILED
  2 = PARTIAL

Environment:
  STATE_DIR  — V2 state directory
  INTERVAL   — candle timeframe (default: 1h)
  SYMBOLS    — optional comma-separated symbol override
"""

import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from data_feed import fetch_closed_klines
from indicators import compute_all
from strategy import evaluate_signal
from strategy_config import (
    get_engine_config,
    get_fee_config,
    get_indicator_params,
)
from compute_hash import compute_strategy_hash
from symbol_specs import get_symbol_specs

from state_manager_v2 import (
    load_or_init_state_v2,
    load_state_v2,
    save_state_v2,
    state_exists_v2,
)
from paper_state_manager import append_events
from paper_engine_v2 import process_symbol_candle
from reconciler_v2 import reconcile_with_data_v2


ROOT = Path(__file__).resolve().parent.parent
V1_STATE_DIR = ROOT / "paper_state_dir"
CRYPTO_WHITELIST_PATH = ROOT / "config" / "crypto_whitelist.json"

MAX_CATCHUP_CANDLES = 100
INITIAL_EQUITY = 984.67

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2


# ============================================================
# Safety guard
# ============================================================

def _guard_state_dir(state_dir: str) -> Path:
    """Reject V1's state dir or any subdir of it."""
    resolved = Path(state_dir).resolve()
    v1_resolved = V1_STATE_DIR.resolve()

    if resolved == v1_resolved:
        raise RuntimeError(
            f"V2 must not use V1 state dir: "
            f"{state_dir} == {V1_STATE_DIR}"
        )

    if v1_resolved in resolved.parents:
        raise RuntimeError(
            f"V2 state dir must not be inside V1 dir: "
            f"{state_dir} is under {V1_STATE_DIR}"
        )

    return resolved


# ============================================================
# Whitelist loading
# ============================================================

def _load_whitelist_ordered(path: Path = None) -> list:
    """Load whitelist symbols while preserving file order."""
    if path is None:
        path = CRYPTO_WHITELIST_PATH

    if not path.exists():
        raise RuntimeError(
            f"crypto_whitelist.json not found at {path}"
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"crypto_whitelist.json is not valid JSON: {e}"
        )

    except OSError as e:
        raise RuntimeError(
            f"crypto_whitelist.json unreadable: {e}"
        )

    if not isinstance(data, dict):
        raise RuntimeError(
            "crypto_whitelist.json: top-level must be a dict"
        )

    symbols = data.get("symbols")

    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError(
            "crypto_whitelist.json: 'symbols' must be non-empty list"
        )

    validated = []
    seen = set()

    for symbol in symbols:
        if (
            not isinstance(symbol, str)
            or not symbol.endswith("-SWAP-USDT")
        ):
            raise RuntimeError(
                f"Invalid symbol in whitelist: {symbol!r}"
            )

        if symbol in seen:
            raise RuntimeError(
                f"Duplicate symbol in whitelist: {symbol!r}"
            )

        seen.add(symbol)
        validated.append(symbol)

    return validated


# ============================================================
# Symbol override
# ============================================================

def _resolve_symbols_override(
    ordered_whitelist: list,
    state_v2: dict,
) -> list:
    """Resolve SYMBOLS override with fail-closed active-symbol checks."""

    raw = os.environ.get("SYMBOLS", "").strip()

    if not raw:
        return ordered_whitelist

    requested = [
        symbol.strip()
        for symbol in raw.split(",")
        if symbol.strip()
    ]

    if not requested:
        raise RuntimeError(
            "SYMBOLS env is set but contains no valid symbols"
        )

    allowed = set(ordered_whitelist)

    not_whitelisted = [
        symbol
        for symbol in requested
        if symbol not in allowed
    ]

    if not_whitelisted:
        raise RuntimeError(
            "SYMBOLS contains non-whitelisted symbols: "
            f"{not_whitelisted}"
        )

    active = [
        symbol
        for symbol, symbol_state
        in state_v2.get("symbols", {}).items()
        if (
            symbol_state.get("position") is not None
            or symbol_state.get("pending_entry") is not None
        )
    ]

    missing_active = [
        symbol
        for symbol in active
        if symbol not in requested
    ]

    if missing_active:
        raise RuntimeError(
            "SYMBOLS excludes currently active symbols: "
            f"{missing_active}. "
            "Refusing to drop management of open positions."
        )

    return requested


# ============================================================
# Candle helpers
# ============================================================

def _find_new_indices(klines: list, lpct) -> list:
    """Return indices newer than last processed candle timestamp."""

    if not klines:
        return []

    if lpct is None:
        return list(range(len(klines)))

    return [
        index
        for index, candle in enumerate(klines)
        if int(candle[0]) > lpct
    ]


# ============================================================
# Legacy isolated processing helper
# ============================================================

def _process_symbol_isolated(
    state_v2: dict,
    symbol: str,
    klines: list,
    specs: dict,
    config: dict,
    allowed_symbols: set,
):
    """Process one symbol on a deepcopy.

    Returns:
        new_state, events, error, n_processed

    On exception, the original state remains unchanged.
    """

    try:
        state_copy = copy.deepcopy(state_v2)

        lpct = state_copy["symbols"][symbol].get(
            "last_processed_candle_ts"
        )

        new_indices = _find_new_indices(
            klines,
            lpct,
        )

        if not new_indices:
            return state_v2, [], None, 0

        if len(new_indices) > MAX_CATCHUP_CANDLES:
            new_indices = new_indices[:MAX_CATCHUP_CANDLES]

        params = get_indicator_params()

        indicators = compute_all(
            klines,
            ema_fast_p=params["ema_fast_p"],
            ema_slow_p=params["ema_slow_p"],
            donchian_p=params["donchian_p"],
            atr_p=params["atr_p"],
        )

        all_events = []

        for index in new_indices:
            signal = evaluate_signal(
                indicators,
                index,
            )

            state_copy, events = process_symbol_candle(
                state_copy,
                symbol,
                klines,
                indicators,
                index,
                specs,
                config,
                signal=signal,
                allowed_symbols=allowed_symbols,
            )

            all_events.extend(events or [])

        return (
            state_copy,
            all_events,
            None,
            len(new_indices),
        )

    except Exception as e:
        return (
            state_v2,
            [],
            f"{type(e).__name__}: {e}",
            0,
        )


# ============================================================
# Shared chronological multi-symbol processing
# ============================================================

def _process_all_symbols(
    state_v2: dict,
    ordered_symbols: list,
    klines_by_symbol: dict,
    engine_config: dict,
    allowed_symbols: set,
    state_dir: str,
):
    """Process all symbols in one shared chronological timeline.

    Returns:
        new_state, total_events_appended, symbol_errors

    Processing rules:
      - One shared working state.
      - Global ascending candle timestamps.
      - Whitelist order for equal timestamps.
      - Catchup cap per symbol.
      - One event append operation.
      - One state save after successful processing.

    This function does not provide a true atomic transaction across
    the event log and state file. Recovery and deduplication must
    be tested separately.
    """

    working_state = copy.deepcopy(state_v2)

    symbol_errors = {}
    timeline = []

    symbol_rank = {
        symbol: rank
        for rank, symbol in enumerate(ordered_symbols)
    }

    # --------------------------------------------------------
    # 1. Prepare symbols and indicators
    # --------------------------------------------------------

    for symbol in ordered_symbols:

        klines = klines_by_symbol.get(symbol)

        symbol_state = (
            working_state
            .get("symbols", {})
            .get(symbol)
        )

        if symbol_state is None:
            raise RuntimeError(
                f"State is missing symbol entry: {symbol}"
            )

        is_active = (
            symbol_state.get("position") is not None
            or symbol_state.get("pending_entry") is not None
        )

        if not klines:

            if is_active:
                raise RuntimeError(
                    f"{symbol}: active position or pending entry "
                    "exists but candle data is unavailable. "
                    "Aborting cycle."
                )

            continue

        try:
            specs = get_symbol_specs(symbol)

            lpct = symbol_state.get(
                "last_processed_candle_ts"
            )

            new_indices = _find_new_indices(
                klines,
                lpct,
            )

            if not new_indices:
                continue

            new_indices = new_indices[
                :MAX_CATCHUP_CANDLES
            ]

            params = get_indicator_params()

            indicators = compute_all(
                klines,
                ema_fast_p=params["ema_fast_p"],
                ema_slow_p=params["ema_slow_p"],
                donchian_p=params["donchian_p"],
                atr_p=params["atr_p"],
            )

            for index in new_indices:

                candle_ts = int(
                    klines[index][0]
                )

                timeline.append(
                    (
                        candle_ts,
                        symbol_rank[symbol],
                        symbol,
                        index,
                        klines,
                        indicators,
                        specs,
                    )
                )

        except Exception as e:

            message = (
                f"{type(e).__name__}: {e}"
            )

            if is_active:
                raise RuntimeError(
                    f"Cannot prepare active symbol "
                    f"{symbol}: {message}"
                ) from e

            symbol_errors[symbol] = message

    # --------------------------------------------------------
    # 2. Nothing to process
    # --------------------------------------------------------

    if not timeline:
        return working_state, 0, symbol_errors

    # --------------------------------------------------------
    # 3. Global chronological ordering
    # --------------------------------------------------------

    timeline.sort(
        key=lambda item: (
            item[0],
            item[1],
        )
    )

    all_events = []

    # --------------------------------------------------------
    # 4. Process all candles using shared portfolio state
    # --------------------------------------------------------

    try:

        for (
            candle_ts,
            _rank,
            symbol,
            index,
            klines,
            indicators,
            specs,
        ) in timeline:

            signal = evaluate_signal(
                indicators,
                index,
            )

            working_state, events = process_symbol_candle(
                working_state,
                symbol,
                klines,
                indicators,
                index,
                specs,
                engine_config,
                signal=signal,
                allowed_symbols=allowed_symbols,
            )

            for event in events or []:

                if not isinstance(event, dict):
                    raise RuntimeError(
                        f"{symbol}: engine returned "
                        "a non-dictionary event"
                    )

                tagged_event = dict(event)

                tagged_event.setdefault(
                    "symbol",
                    symbol,
                )

                tagged_event.setdefault(
                    "candle_ts_ms",
                    candle_ts,
                )

                all_events.append(
                    tagged_event
                )

    except Exception as e:

        raise RuntimeError(
            "Chronological processing failed: "
            f"{type(e).__name__}: {e}"
        ) from e

    # --------------------------------------------------------
    # 5. Append events before state persistence
    # --------------------------------------------------------

    total_events = 0

    if all_events:

        try:

            strategy_hash = compute_strategy_hash()

            appended, _skipped = append_events(
                state_dir,
                all_events,
                strategy_hash,
            )

            total_events = appended

        except Exception as e:

            raise RuntimeError(
                "append_events failed; state was not committed: "
                f"{type(e).__name__}: {e}"
            ) from e

    # --------------------------------------------------------
    # 6. Save shared state once
    # --------------------------------------------------------

    try:

        save_state_v2(
            state_dir,
            working_state,
        )

    except Exception as e:

        raise RuntimeError(
            "save_state failed after chronological processing: "
            f"{type(e).__name__}: {e}"
        ) from e

    return (
        working_state,
        total_events,
        symbol_errors,
    )


# ============================================================
# Reporting helpers
# ============================================================

def _active_positions(state_v2: dict) -> list:
    """Return open positions as (symbol, position) pairs."""

    out = []

    for symbol, symbol_state in (
        state_v2.get("symbols", {}).items()
    ):

        if symbol_state.get("position") is not None:
            out.append(
                (
                    symbol,
                    symbol_state["position"],
                )
            )

    return out


def _active_pendings(state_v2: dict) -> list:
    """Return pending entries as (symbol, pending) pairs."""

    out = []

    for symbol, symbol_state in (
        state_v2.get("symbols", {}).items()
    ):

        if symbol_state.get("pending_entry") is not None:
            out.append(
                (
                    symbol,
                    symbol_state["pending_entry"],
                )
            )

    return out


# ============================================================
# Main
# ============================================================

def main() -> int:

    state_dir = os.environ.get(
        "STATE_DIR",
        "./multi_symbol_state_dir",
    )

    interval = os.environ.get(
        "INTERVAL",
        "1h",
    )

    start_iso = datetime.now(
        timezone.utc
    ).isoformat()

    print("=" * 70)
    print("MULTI-SYMBOL RUN (V2)")
    print("=" * 70)

    print(f"  STATE_DIR : {state_dir}")
    print(f"  INTERVAL  : {interval}")

    # --------------------------------------------------------
    # 1. State directory safety guard
    # --------------------------------------------------------

    try:

        resolved_state_dir = _guard_state_dir(
            state_dir
        )

        print(
            f"  resolved  : {resolved_state_dir}"
        )

    except RuntimeError as e:

        print(f"GUARD ERROR: {e}")
        return EXIT_FAILED

    # --------------------------------------------------------
    # 2. Load whitelist
    # --------------------------------------------------------

    try:

        whitelist_ordered = _load_whitelist_ordered()

        print(
            f"  whitelist : {len(whitelist_ordered)} symbols"
        )

    except RuntimeError as e:

        print(f"WHITELIST ERROR: {e}")
        return EXIT_FAILED

    # --------------------------------------------------------
    # 3. Load or initialize V2 state
    # --------------------------------------------------------

    try:

        if state_exists_v2(state_dir):

            state_v2 = load_state_v2(
                state_dir,
                allowed_symbols=whitelist_ordered,
            )

            print("State loaded")

        else:

            state_v2 = load_or_init_state_v2(
                state_dir,
                initial_equity=INITIAL_EQUITY,
                symbols=whitelist_ordered,
                max_concurrent_positions=1,
                allowed_symbols=whitelist_ordered,
            )

            save_state_v2(
                state_dir,
                state_v2,
            )

            print("State initialized")

    except Exception as e:

        print(
            f"STATE ERROR: {type(e).__name__}: {e}"
        )

        return EXIT_FAILED

    # --------------------------------------------------------
    # 4. Resolve symbol override
    # --------------------------------------------------------

    try:

        ordered_symbols = _resolve_symbols_override(
            whitelist_ordered,
            state_v2,
        )

        allowed_symbols = set(
            whitelist_ordered
        )

        print(
            f"  processing: {ordered_symbols}"
        )

    except RuntimeError as e:

        print(
            f"SYMBOLS OVERRIDE ERROR: {e}"
        )

        return EXIT_FAILED

    print(
        f"  equity    : {state_v2.get('equity')}"
    )

    print()

    # --------------------------------------------------------
    # 5. Fetch candle data
    # --------------------------------------------------------

    klines_by_symbol = {}
    fetch_errors = {}

    for symbol in ordered_symbols:

        try:

            klines = fetch_closed_klines(
                symbol,
                interval,
                limit=300,
            )

            klines_by_symbol[symbol] = klines

            print(
                f"{symbol}: {len(klines)} klines"
            )

        except Exception as e:

            klines_by_symbol[symbol] = None

            fetch_errors[symbol] = (
                f"{type(e).__name__}: {e}"
            )

            print(
                f"{symbol}: fetch failed ({e})"
            )

    print()

    # --------------------------------------------------------
    # 6. Reconciliation gate
    # --------------------------------------------------------

    try:

        now_ms = int(
            time.time() * 1000
        )

        rec = reconcile_with_data_v2(
            state_v2,
            klines_by_symbol,
            interval,
            now_ms,
        )

        print(
            f"Reconciler: {rec['status']} — {rec['summary']}"
        )

        if rec["status"] == "ERROR":

            print(
                "Reconciler ERROR — ABORTING. "
                "No symbol will be processed."
            )

            for check in rec["checks"]:

                if (
                    not check["ok"]
                    and check["level"] == "error"
                ):

                    print(
                        f"  {check['name']}: {check['detail']}"
                    )

            return EXIT_FAILED

        elif rec["status"] == "WARNING":

            print(
                "Reconciler WARNING — review required:"
            )

            for check in rec["checks"]:

                if (
                    not check["ok"]
                    and check["level"] == "warning"
                ):

                    print(
                        f"  {check['name']}: {check['detail']}"
                    )

    except Exception as e:

        print(
            f"RECONCILER CRASH: {type(e).__name__}: {e}"
        )

        return EXIT_FAILED

    print()

    # --------------------------------------------------------
    # 7. Engine and fee configuration
    # --------------------------------------------------------

    try:

        engine_config = get_engine_config()

        fee_cfg = get_fee_config()

        engine_config["open_taker_fee"] = (
            fee_cfg["open_taker_fee"]
        )

        engine_config["close_taker_fee"] = (
            fee_cfg["close_taker_fee"]
        )

    except Exception as e:

        print(
            f"ENGINE CONFIG ERROR: {type(e).__name__}: {e}"
        )

        return EXIT_FAILED

    # --------------------------------------------------------
    # 8. Shared chronological processing
    # --------------------------------------------------------

    try:

        (
            state_v2,
            total_events,
            symbol_errors,
        ) = _process_all_symbols(
            state_v2,
            ordered_symbols,
            klines_by_symbol,
            engine_config,
            allowed_symbols,
            state_dir,
        )

    except RuntimeError as e:

        print(
            f"FATAL PROCESSING ERROR: {e}"
        )

        return EXIT_FAILED

    # --------------------------------------------------------
    # 9. Summary
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("SUMMARY")

    print(
        f"  equity          : {state_v2.get('equity')}"
    )

    positions = _active_positions(
        state_v2
    )

    pendings = _active_pendings(
        state_v2
    )

    if positions:

        for symbol, position in positions:

            print(
                f"  position[{symbol}] : "
                f"{position.get('side')} "
                f"size={position.get('size')}"
            )

    else:

        print(
            "  positions       : none"
        )

    if pendings:

        for symbol, pending in pendings:

            print(
                f"  pending[{symbol}] : "
                f"{pending.get('side')}"
            )

    else:

        print(
            "  pendings        : none"
        )

    print(
        f"  total_events    : {total_events}"
    )

    total_errors = (
        len(fetch_errors)
        + len(symbol_errors)
    )

    if total_errors:

        print(
            f"  errors          : {total_errors}"
        )

        for symbol, error in fetch_errors.items():

            print(
                f"  fetch[{symbol}] : {error}"
            )

        for symbol, error in symbol_errors.items():

            print(
                f"  proc [{symbol}] : {error}"
            )

    print(
        f"  started         : {start_iso}"
    )

    print(
        f"  finished        : "
        f"{datetime.now(timezone.utc).isoformat()}"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # 10. Exit code
    # --------------------------------------------------------

    if total_errors == 0:

        print("OK")
        return EXIT_OK

    elif total_errors < len(ordered_symbols):

        print(
            "PARTIAL — some symbols failed"
        )

        return EXIT_PARTIAL

    else:

        print(
            "ALL SYMBOLS FAILED"
        )

        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())