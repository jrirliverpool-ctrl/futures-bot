
import copy
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import process_candle as _v1_process_candle

logger = logging.getLogger(__name__)

_V1_STATE_FIELDS = (
    "initial_equity",
    "equity",
    "peak_equity",
    "daily_loss",
    "weekly_loss",
    "loss_day_key",
    "loss_week_key",
    "circuit_breaker_active",
)

_SYMBOL_FIELDS = (
    "position",
    "pending_entry",
    "cooldown_until_index",
    "last_processed_candle_ts",
    "last_funding_ts",
)


def _extract_v1_slice(state_v2: dict, symbol: str) -> dict:
    """Build an isolated V1-compatible state for one V2 symbol."""
    if not isinstance(state_v2, dict):
        raise ValueError("state_v2 must be a dictionary")

    symbols = state_v2.get("symbols")
    if not isinstance(symbols, dict):
        raise ValueError("V2 symbols must be a dictionary")

    if symbol not in symbols:
        raise ValueError(f"Symbol {symbol!r} not in V2 state")

    sym = symbols[symbol]
    if not isinstance(sym, dict):
        raise ValueError(f"Invalid state for symbol {symbol!r}")

    result = {
        key: copy.deepcopy(state_v2[key])
        for key in _V1_STATE_FIELDS
        if key in state_v2
    }

    for key in _SYMBOL_FIELDS:
        if key in sym:
            result[key] = copy.deepcopy(sym[key])

    return result


def _apply_v1_result_to_v2(
    state_v2: dict,
    symbol: str,
    state_v1_after: dict,
) -> None:
    """
    Merge V1 engine output into the shared V2 portfolio state.

    Missing fields in the V1 result are deliberately preserved in V2.
    This prevents partial V1 results from overwriting shared portfolio
    metadata or raising KeyError for optional fields.
    """
    if not isinstance(state_v2, dict):
        raise ValueError("state_v2 must be a dictionary")

    if not isinstance(state_v1_after, dict):
        raise ValueError("state_v1_after must be a dictionary")

    symbols = state_v2.get("symbols")
    if not isinstance(symbols, dict):
        raise ValueError("V2 symbols must be a dictionary")

    if symbol not in symbols:
        raise ValueError(f"Symbol {symbol!r} not in V2 state")

    sym = symbols[symbol]
    if not isinstance(sym, dict):
        raise ValueError(f"Invalid state for symbol {symbol!r}")

    # Update only shared fields actually returned by V1.
    for key in _V1_STATE_FIELDS:
        if key in state_v1_after:
            state_v2[key] = copy.deepcopy(state_v1_after[key])

    # Update only this symbol's fields actually returned by V1.
    for key in _SYMBOL_FIELDS:
        if key in state_v1_after:
            sym[key] = copy.deepcopy(state_v1_after[key])


def _count_portfolio_reservations(
    state_v2: dict,
    exclude_symbol: str = None,
) -> int:
    """Count symbols holding either a position or a pending entry.

    A symbol is counted once even if malformed data contains both.
    """
    symbols = state_v2.get("symbols")

    if not isinstance(symbols, dict):
        raise ValueError("V2 symbols must be a dictionary")

    count = 0

    for name, sym_state in symbols.items():
        if name == exclude_symbol:
            continue

        if not isinstance(sym_state, dict):
            raise ValueError(f"Invalid symbol state: {name}")

        if (
            sym_state.get("position") is not None
            or sym_state.get("pending_entry") is not None
        ):
            count += 1

    return count


def _portfolio_has_room_for_pending(
    state_v2: dict,
    symbol: str,
) -> bool:
    """Pre-signal gate. Current symbol must have no position or pending."""
    symbols = state_v2.get("symbols", {})

    if not isinstance(symbols, dict) or symbol not in symbols:
        return False

    if state_v2.get("circuit_breaker_active", False):
        return False

    current = symbols[symbol]

    if not isinstance(current, dict):
        return False

    if current.get("position") is not None:
        return False

    if current.get("pending_entry") is not None:
        return False

    maximum = state_v2.get("max_concurrent_positions", 1)

    if isinstance(maximum, bool) or not isinstance(maximum, int):
        return False

    if maximum < 1:
        return False

    return (
        _count_portfolio_reservations(
            state_v2,
            exclude_symbol=symbol,
        )
        < maximum
    )


def _portfolio_allows_entry(
    state_v2: dict,
    symbol: str,
) -> bool:
    """Execution-time gate.

    The current symbol's pending entry is allowed: it is the reservation
    being executed. Other symbols' positions/pending entries consume capacity.
    """
    symbols = state_v2.get("symbols", {})

    if not isinstance(symbols, dict) or symbol not in symbols:
        return False

    if state_v2.get("circuit_breaker_active", False):
        return False

    current = symbols[symbol]

    if not isinstance(current, dict):
        return False

    if current.get("position") is not None:
        return False

    maximum = state_v2.get("max_concurrent_positions", 1)

    if isinstance(maximum, bool) or not isinstance(maximum, int):
        return False

    if maximum < 1:
        return False

    return (
        _count_portfolio_reservations(
            state_v2,
            exclude_symbol=symbol,
        )
        < maximum
    )


def _sync_circuit_breaker(
    state_v2: dict,
    was_active: bool,
    events: list,
) -> None:
    """Synchronize V2 circuit-breaker metadata on state transitions."""
    is_active = bool(
        state_v2.get("circuit_breaker_active", False)
    )

    if is_active and not was_active:
        reason = next(
            (
                event.get("reason")
                for event in events
                if event.get("type") == "CIRCUIT_BREAKER"
            ),
            "UNKNOWN",
        )

        state_v2["circuit_breaker_reason"] = reason

        state_v2["circuit_breaker_activated_at"] = (
            datetime.now(timezone.utc).isoformat()
        )

    elif was_active and not is_active:
        state_v2["circuit_breaker_reason"] = None
        state_v2["circuit_breaker_activated_at"] = None


def load_allowed_symbols(path) -> set:
    """Load and strictly validate the symbol whitelist."""
    path = Path(path)

    if not path.exists():
        raise RuntimeError(
            f"Whitelist file not found: {path}"
        )

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot read whitelist: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "Whitelist root must be a JSON object"
        )

    symbols = data.get("symbols")

    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError(
            "Whitelist must contain a non-empty list"
        )

    result = set()

    for symbol in symbols:
        if (
            not isinstance(symbol, str)
            or not re.fullmatch(
                r"[A-Z0-9]+-SWAP-USDT",
                symbol,
            )
        ):
            raise RuntimeError(
                f"Invalid symbol in whitelist: {symbol!r}"
            )

        if symbol in result:
            raise RuntimeError(
                f"Duplicate symbol in whitelist: {symbol}"
            )

        result.add(symbol)

    return result


def process_symbol_candle(
    state_v2: dict,
    symbol: str,
    klines: list,
    indicators: dict,
    i: int,
    specs: dict,
    config: dict,
    signal=None,
    execution_callback=None,
    exit_callback=None,
    allowed_symbols=None,
) -> tuple:
    """Process one symbol through the V1 engine under V2 portfolio gates."""
    if not isinstance(state_v2, dict):
        raise ValueError(
            "state_v2 must be a dictionary"
        )

    symbols = state_v2.get("symbols")

    if not isinstance(symbols, dict):
        raise ValueError(
            "V2 state has no valid symbols dictionary"
        )

    if symbol not in symbols:
        raise ValueError(
            f"Symbol {symbol!r} not in V2 state"
        )

    if (
        allowed_symbols is not None
        and symbol not in allowed_symbols
    ):
        raise ValueError(
            f"Symbol {symbol!r} not in allowed_symbols"
        )

    sym_state = symbols[symbol]

    if not isinstance(sym_state, dict):
        raise ValueError(
            f"Invalid state for symbol {symbol!r}"
        )

    was_cb_active = bool(
        state_v2.get("circuit_breaker_active", False)
    )

    blocked_events = []

    # Do not let V1 create a new pending entry when portfolio capacity
    # is already reserved or the circuit breaker is active.
    effective_signal = signal

    if (
        signal is not None
        and isinstance(signal, dict)
        and signal.get("signal") in ("LONG", "SHORT")
        and not _portfolio_has_room_for_pending(
            state_v2,
            symbol,
        )
    ):
        blocked_events.append({
            "type": "SIGNAL_BLOCKED_PORTFOLIO",
            "symbol": symbol,
            "reason": (
                "circuit_breaker_active"
                if state_v2.get(
                    "circuit_breaker_active",
                    False,
                )
                else "portfolio_capacity_or_symbol_reserved"
            ),
        })

        effective_signal = None

    state_v1 = _extract_v1_slice(
        state_v2,
        symbol,
    )

    def combined_execution_callback(info, state_ref):
        if info.get("intent") == "ENTRY":
            if not _portfolio_allows_entry(
                state_v2,
                symbol,
            ):
                logger.warning(
                    "V2 portfolio gate blocked ENTRY for %s",
                    symbol,
                )
                return False

        if execution_callback is not None:
            return bool(
                execution_callback(
                    info,
                    state_ref,
                )
            )

        return True

    state_v1_after, v1_events = _v1_process_candle(
        state_v1,
        klines,
        indicators,
        i,
        specs,
        config,
        signal=effective_signal,
        execution_callback=combined_execution_callback,
        exit_callback=exit_callback,
    )

    _apply_v1_result_to_v2(
        state_v2,
        symbol,
        state_v1_after,
    )

    all_events = blocked_events + v1_events

    _sync_circuit_breaker(
        state_v2,
        was_active=was_cb_active,
        events=all_events,
    )

    return state_v2, all_events
