"""run_multi_symbol.py — Multi-symbol orchestrator for ApexQuant V2.

SAFETY GUARANTEES:
  1. state_dir must NOT be paper_state_dir (or inside it). Exit 1 otherwise.
  2. If reconciler returns ERROR, the entire cycle is aborted BEFORE any
     symbol is processed.
  3. Each symbol is processed on a DEEPCOPY of state. On any failure, its
     changes are discarded — no partial state committed.
  4. append_events is called BEFORE state commit. If append fails, state
     is not updated (rollback). Event deduplication (fingerprint) prevents
     double-writes on retry.
  5. Catchup is capped at MAX_CATCHUP_CANDLES per symbol per cycle.
  6. V1 state (paper_state_dir) is never touched.

LIMITATIONS (documented, not solved yet):
  - STATE PERSISTENCE: each GitHub Actions run starts fresh. The workflow
    uploads state as artifact, but the NEXT run does NOT auto-restore it.
    This workflow is MANUAL-ONLY for a single validation cycle. Do NOT
    schedule it (no cron) until state persistence is designed, tested,
    and approved.
  - Rate limit: 10 symbols × 300 candles = ~10 requests per cycle. Fine
    for manual testing. Cron usage would need backoff/rate-limit design.

Exit codes:
  0 = OK (all symbols processed cleanly, no fetch or process errors)
  1 = FAILED (fatal: guard, reconciler ERROR, state save failure)
  2 = PARTIAL (some symbols failed, others succeeded — review required)

Environment:
  STATE_DIR  — V2 state dir (default: ./multi_symbol_state_dir)
  INTERVAL   — candle timeframe (default: 1h)
  SYMBOLS    — optional comma-separated override. If set, only these
               symbols are processed in the cycle. Every listed symbol
               MUST be in crypto_whitelist.json, AND all currently active
               symbols (with position or pending_entry) must be included;
               otherwise main() exits 1 (fail-closed).
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
    """Reject V1's state dir or any subdir of it. Returns resolved path."""
    resolved = Path(state_dir).resolve()
    v1_resolved = V1_STATE_DIR.resolve()

    if resolved == v1_resolved:
        raise RuntimeError(
            f"V2 must not use V1 state dir: {state_dir} == {V1_STATE_DIR}"
        )
    if v1_resolved in resolved.parents:
        raise RuntimeError(
            f"V2 state dir must not be inside V1 dir: "
            f"{state_dir} is under {V1_STATE_DIR}"
        )
    return resolved


# ============================================================
# Whitelist loading (order-preserving)
# ============================================================

def _load_whitelist_ordered(path: Path = None) -> list:
    """Load crypto_whitelist.json in file order. All parse errors wrapped."""
    if path is None:
        path = CRYPTO_WHITELIST_PATH

    if not path.exists():
        raise RuntimeError(f"crypto_whitelist.json not found at {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"crypto_whitelist.json is not valid JSON: {e}")
    except OSError as e:
        raise RuntimeError(f"crypto_whitelist.json unreadable: {e}")

    if not isinstance(data, dict):
        raise RuntimeError("crypto_whitelist.json: top-level must be a dict")

    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError("crypto_whitelist.json: 'symbols' must be non-empty list")

    validated = []
    seen = set()
    for s in symbols:
        if not isinstance(s, str) or not s.endswith("-SWAP-USDT"):
            raise RuntimeError(f"Invalid symbol in whitelist: {s!r}")
        if s in seen:
            raise RuntimeError(f"Duplicate symbol in whitelist: {s!r}")
        seen.add(s)
        validated.append(s)

    return validated


def _resolve_symbols_override(ordered_whitelist: list, state_v2: dict) -> list:
    """If SYMBOLS env is set, restrict. Fail-closed on unsafe combos.

    - Every SYMBOLS entry must be in whitelist.
    - Every currently active symbol (position or pending) must be included.
    """
    raw = os.environ.get("SYMBOLS", "").strip()
    if not raw:
        return ordered_whitelist

    requested = [s.strip() for s in raw.split(",") if s.strip()]
    if not requested:
        raise RuntimeError("SYMBOLS env is set but contains no valid symbols")

    allowed = set(ordered_whitelist)
    not_whitelisted = [s for s in requested if s not in allowed]
    if not_whitelisted:
        raise RuntimeError(
            f"SYMBOLS contains non-whitelisted symbols: {not_whitelisted}"
        )

    active = [
        sym for sym, st in state_v2.get("symbols", {}).items()
        if st.get("position") is not None or st.get("pending_entry") is not None
    ]
    missing_active = [s for s in active if s not in requested]
    if missing_active:
        raise RuntimeError(
            f"SYMBOLS excludes currently active symbols: {missing_active}. "
            f"Refusing to drop management of open positions."
        )

    return requested


# ============================================================
# Candle helpers
# ============================================================

def _find_new_indices(klines: list, lpct) -> list:
    if not klines:
        return []
    if lpct is None:
        return list(range(len(klines)))
    return [i for i, c in enumerate(klines) if int(c[0]) > lpct]


# ============================================================
# Per-symbol processing (isolated)
# ============================================================

def _process_symbol_isolated(
    state_v2: dict,
    symbol: str,
    klines: list,
    specs: dict,
    config: dict,
    allowed_symbols: set,
):
    """Process one symbol on a deepcopy. Returns (new_state, events, error, n_processed).

    On any exception:
      - Returns (state_v2, [], error, 0)
      - state_v2 is the ORIGINAL object, unmodified.
    """
    try:
        state_copy = copy.deepcopy(state_v2)

        lpct = state_copy["symbols"][symbol].get("last_processed_candle_ts")
        new_indices = _find_new_indices(klines, lpct)
        if not new_indices:
            return state_v2, [], None, 0

        if len(new_indices) > MAX_CATCHUP_CANDLES:
            new_indices = new_indices[:MAX_CATCHUP_CANDLES]

        p = get_indicator_params()
        indicators = compute_all(
            klines,
            ema_fast_p=p["ema_fast_p"],
            ema_slow_p=p["ema_slow_p"],
            donchian_p=p["donchian_p"],
            atr_p=p["atr_p"],
        )

        all_events = []
        for idx in new_indices:
            sig = evaluate_signal(indicators, idx)
            state_copy, events = process_symbol_candle(
                state_copy, symbol, klines, indicators, idx,
                specs, config,
                signal=sig,
                allowed_symbols=allowed_symbols,
            )
            all_events.extend(events)

        return state_copy, all_events, None, len(new_indices)
    except Exception as e:
        return state_v2, [], f"{type(e).__name__}: {e}", 0


def _process_all_symbols(
    state_v2: dict,
    ordered_symbols: list,
    klines_by_symbol: dict,
    engine_config: dict,
    allowed_symbols: set,
    state_dir: str,
):
    """Iterate symbols. Returns (new_state, total_events_appended, errors_dict).

    Isolation:
      - Each symbol's processing runs on a deepcopy.
      - append_events FIRST, state commit SECOND.
      - If append fails, that symbol's state is discarded.
      - If save fails, raises RuntimeError (fatal — caller returns EXIT_FAILED).
    """
    symbol_errors = {}
    total_events = 0

    for sym in ordered_symbols:
        klines = klines_by_symbol.get(sym)
        if not klines:
            continue

        try:
            specs = get_symbol_specs(sym)
        except Exception as e:
            symbol_errors[sym] = f"specs: {type(e).__name__}: {e}"
            continue

        new_state, events, err, n_processed = _process_symbol_isolated(
            state_v2, sym, klines, specs, engine_config, allowed_symbols,
        )

        if err:
            symbol_errors[sym] = err
            continue

        if events:
            try:
                strategy_hash = compute_strategy_hash()
                appended, _skipped = append_events(state_dir, events, strategy_hash)
                total_events += appended
            except Exception as e:
                symbol_errors[sym] = f"append_events: {type(e).__name__}: {e}"
                continue  # state_v2 NOT updated — rollback

        # Commit state for this symbol
        state_v2 = new_state

        # Save state (fatal if fails)
        try:
            save_state_v2(state_dir, state_v2)
        except Exception as e:
            raise RuntimeError(f"save_state failed for {sym}: {e}") from e

    return state_v2, total_events, symbol_errors


# ============================================================
# Reporting
# ============================================================

def _active_positions(state_v2: dict) -> list:
    """Return list of (symbol, position_dict) for open positions."""
    out = []
    for sym, st in state_v2.get("symbols", {}).items():
        if st.get("position") is not None:
            out.append((sym, st["position"]))
    return out


def _active_pendings(state_v2: dict) -> list:
    out = []
    for sym, st in state_v2.get("symbols", {}).items():
        if st.get("pending_entry") is not None:
            out.append((sym, st["pending_entry"]))
    return out


# ============================================================
# Main
# ============================================================

def main() -> int:
    state_dir = os.environ.get("STATE_DIR", "./multi_symbol_state_dir")
    interval = os.environ.get("INTERVAL", "1h")
    start_iso = datetime.now(timezone.utc).isoformat()

    print("=" * 70)
    print("🌐 MULTI-SYMBOL RUN (V2)")
    print("=" * 70)
    print(f"  STATE_DIR : {state_dir}")
    print(f"  INTERVAL  : {interval}")

    # === 1. Guard state dir ===
    try:
        resolved_state_dir = _guard_state_dir(state_dir)
        print(f"  resolved  : {resolved_state_dir}")
    except RuntimeError as e:
        print(f"❌ GUARD: {e}")
        return EXIT_FAILED

    # === 2. Load whitelist ===
    try:
        whitelist_ordered = _load_whitelist_ordered()
        print(f"  whitelist : {len(whitelist_ordered)} symbols")
    except RuntimeError as e:
        print(f"❌ WHITELIST: {e}")
        return EXIT_FAILED

    # === 3. Load or init state ===
    try:
        if state_exists_v2(state_dir):
            state_v2 = load_state_v2(state_dir, allowed_symbols=whitelist_ordered)
            print("✅ State loaded")
        else:
            state_v2 = load_or_init_state_v2(
                state_dir,
                initial_equity=INITIAL_EQUITY,
                symbols=whitelist_ordered,
                max_concurrent_positions=1,
                allowed_symbols=whitelist_ordered,
            )
            save_state_v2(state_dir, state_v2)
            print("✅ State initialized")
    except Exception as e:
        print(f"❌ STATE: {type(e).__name__}: {e}")
        return EXIT_FAILED

    # === 4. Resolve SYMBOLS override (after state, before fetch) ===
    try:
        ordered_symbols = _resolve_symbols_override(whitelist_ordered, state_v2)
        allowed_symbols = set(whitelist_ordered)  # whitelist stays full
        print(f"  processing: {ordered_symbols}")
    except RuntimeError as e:
        print(f"❌ SYMBOLS OVERRIDE: {e}")
        return EXIT_FAILED

    print(f"  equity    : {state_v2.get('equity')}")
    print()

    # === 5. Fetch klines per symbol ===
    klines_by_symbol = {}
    fetch_errors = {}
    for sym in ordered_symbols:
        try:
            klines = fetch_closed_klines(sym, interval, limit=300)
            klines_by_symbol[sym] = klines
            print(f"📥 {sym}: {len(klines)} klines")
        except Exception as e:
            klines_by_symbol[sym] = None
            fetch_errors[sym] = f"{type(e).__name__}: {e}"
            print(f"⚠️  {sym}: fetch failed ({e})")
    print()

    # === 6. Reconciler gate (BEFORE any processing) ===
    try:
        now_ms = int(time.time() * 1000)
        rec = reconcile_with_data_v2(state_v2, klines_by_symbol, interval, now_ms)
        print(f"🔍 Reconciler: {rec['status']} — {rec['summary']}")

        if rec["status"] == "ERROR":
            print("\n❌ Reconciler ERROR — ABORTING (no symbol will be processed)")
            for c in rec["checks"]:
                if not c["ok"] and c["level"] == "error":
                    print(f"   ❌ {c['name']}: {c['detail']}")
            return EXIT_FAILED
        elif rec["status"] == "WARNING":
            print("⚠️  Reconciler WARNING (review):")
            for c in rec["checks"]:
                if not c["ok"] and c["level"] == "warning":
                    print(f"   ⚠️  {c['name']}: {c['detail']}")
    except Exception as e:
        print(f"❌ RECONCILER CRASH: {type(e).__name__}: {e}")
        return EXIT_FAILED
    print()

    # === 7. Engine config ===
    engine_config = get_engine_config()
    fee_cfg = get_fee_config()
    engine_config["open_taker_fee"] = fee_cfg["open_taker_fee"]
    engine_config["close_taker_fee"] = fee_cfg["close_taker_fee"]

    # === 8. Process all symbols ===
    try:
        state_v2, total_events, symbol_errors = _process_all_symbols(
            state_v2, ordered_symbols, klines_by_symbol,
            engine_config, allowed_symbols, state_dir,
        )
    except RuntimeError as e:
        print(f"\n❌ FATAL: {e}")
        return EXIT_FAILED

    # === 9. Summary ===
    print()
    print("=" * 70)
    print("📌 SUMMARY")
    print(f"  equity          : {state_v2.get('equity')}")

    positions = _active_positions(state_v2)
    pendings = _active_pendings(state_v2)

    if positions:
        for sym, pos in positions:
            print(f"  position[{sym}] : {pos.get('side')} size={pos.get('size')}")
    else:
        print("  positions       : none")

    if pendings:
        for sym, pend in pendings:
            print(f"  pending[{sym}]  : {pend.get('side')}")
    else:
        print("  pendings        : none")

    print(f"  total_events    : {total_events}")

    total_errors = len(fetch_errors) + len(symbol_errors)
    if total_errors:
        print(f"  errors          : {total_errors}")
        for sym, err in fetch_errors.items():
            print(f"    fetch[{sym}] : {err}")
        for sym, err in symbol_errors.items():
            print(f"    proc [{sym}] : {err}")

    print(f"  started         : {start_iso}")
    print(f"  finished        : {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # === 10. Exit code ===
    if total_errors == 0:
        print("✅ OK")
        return EXIT_OK
    elif total_errors < len(ordered_symbols):
        print("⚠️  PARTIAL — some symbols failed")
        return EXIT_PARTIAL
    else:
        print("❌ ALL SYMBOLS FAILED")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())