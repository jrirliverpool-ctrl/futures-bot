
import copy
import json
import logging
import math
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


def _finite(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _positive_config(config, key, default=0.0):
    value = config.get(key, default)

    if not _finite(value) or value < 0:
        return None

    return float(value)


def _extract_v1_slice(state_v2, symbol):
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


def _apply_v1_result_to_v2(state_v2, symbol, state_v1_after):
    if not isinstance(state_v2, dict):
        raise ValueError("state_v2 must be a dictionary")

    if not isinstance(state_v1_after, dict):
        raise ValueError("state_v1_after must be a dictionary")

    symbols = state_v2.get("symbols")

    if not isinstance(symbols, dict) or symbol not in symbols:
        raise ValueError(f"Symbol {symbol!r} not in V2 state")

    sym = symbols[symbol]

    if not isinstance(sym, dict):
        raise ValueError(f"Invalid state for symbol {symbol!r}")

    for key in _V1_STATE_FIELDS:
        if key in state_v1_after:
            state_v2[key] = copy.deepcopy(state_v1_after[key])

    for key in _SYMBOL_FIELDS:
        if key in state_v1_after:
            sym[key] = copy.deepcopy(state_v1_after[key])


def _count_portfolio_reservations(state_v2, exclude_symbol=None):
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


def _portfolio_has_room_for_pending(state_v2, symbol):
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


def _portfolio_allows_entry(state_v2, symbol):
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


# ============================================================
# PORTFOLIO RISK ENGINE
# ============================================================

def _portfolio_risk_limit(state_v2):
    equity = state_v2.get("equity")
    fraction = state_v2.get("max_portfolio_risk")

    if not _finite(equity) or equity <= 0:
        return None

    if not _finite(fraction) or not (0 < fraction <= 1):
        return None

    return float(equity) * float(fraction)


def _position_risk_usd(position, config):
    """
    Calculate remaining position risk from its original risk metadata.

    The original risk_usd stores:
        original_qty * original_stop_distance * contract_multiplier

    This lets us recover the multiplier without changing the V2
    state schema or requiring a fresh exchange metadata lookup.
    """

    if not isinstance(position, dict):
        return None

    side = position.get("side")

    if side not in ("LONG", "SHORT"):
        return None

    entry = position.get("entry_price")
    initial_sl = position.get("initial_sl")
    current_sl = position.get("current_sl")
    size = position.get("size")
    remaining = position.get("remaining_size")
    original_risk = position.get("risk_usd")

    for value in (
        entry,
        initial_sl,
        current_sl,
        size,
        remaining,
        original_risk,
    ):
        if not _finite(value):
            return None

    if (
        entry <= 0
        or initial_sl <= 0
        or current_sl <= 0
        or size <= 0
        or remaining < 0
        or remaining > size
        or original_risk <= 0
    ):
        return None

    initial_distance = abs(entry - initial_sl)

    if initial_distance <= 0:
        return None

    if side == "LONG" and initial_sl >= entry:
        return None

    if side == "SHORT" and initial_sl <= entry:
        return None

    multiplier = original_risk / (size * initial_distance)

    if not _finite(multiplier) or multiplier <= 0:
        return None

    if side == "LONG":
        adverse_distance = max(0.0, entry - current_sl)
    else:
        adverse_distance = max(0.0, current_sl - entry)

    gross_risk = (
        remaining
        * adverse_distance
        * multiplier
    )

    notional = remaining * entry * multiplier

    close_fee = _positive_config(
        config,
        "close_taker_fee",
    )

    slippage = _positive_config(
        config,
        "slippage_pct",
    )

    if close_fee is None or slippage is None:
        return None

    exit_cost = notional * (close_fee + slippage)

    result = gross_risk + exit_cost

    return result if _finite(result) and result >= 0 else None


def _pending_risk_reservation_usd(pending, equity, config):
    """
    Reserve risk for an unexecuted pending entry.

    Uses the risk budget and estimated notional to include both
    entry/exit fees and two-sided slippage.

    This is deliberately conservative: the final execution gate
    recalculates the exact risk from the actual entry information.
    """

    if not isinstance(pending, dict):
        return None

    side = pending.get("side")

    if side not in ("LONG", "SHORT"):
        return None

    close_price = pending.get("close_at_signal")
    atr = pending.get("atr_at_signal")

    if not _finite(equity) or equity <= 0:
        return None

    if (
        not _finite(close_price)
        or close_price <= 0
        or not _finite(atr)
        or atr <= 0
    ):
        return None

    risk_per_trade = _positive_config(
        config,
        "risk_per_trade",
    )

    sl_mult = _positive_config(
        config,
        "sl_atr_mult",
    )

    open_fee = _positive_config(
        config,
        "open_taker_fee",
    )

    close_fee = _positive_config(
        config,
        "close_taker_fee",
    )

    slippage = _positive_config(
        config,
        "slippage_pct",
    )

    if any(
        value is None
        for value in (
            risk_per_trade,
            sl_mult,
            open_fee,
            close_fee,
            slippage,
        )
    ):
        return None

    if risk_per_trade <= 0 or sl_mult <= 0:
        return None

    entry_estimate = (
        close_price * (1.0 + slippage)
        if side == "LONG"
        else close_price * (1.0 - slippage)
    )

    if entry_estimate <= 0:
        return None

    stop_distance = atr * sl_mult

    if stop_distance <= 0 or stop_distance >= entry_estimate:
        return None

    risk_budget = equity * risk_per_trade

    estimated_notional = (
        risk_budget * entry_estimate / stop_distance
    )

    execution_cost = estimated_notional * (
        open_fee + close_fee + 2.0 * slippage
    )

    result = risk_budget + execution_cost

    return result if _finite(result) and result > 0 else None


def _new_entry_risk_usd(info, specs, config):
    """
    Calculate risk for the actual proposed entry.

    Called immediately before the V1 engine accepts the entry.
    """

    if not isinstance(info, dict) or not isinstance(specs, dict):
        return None

    side = info.get("side")
    qty = info.get("qty")
    price = info.get("price")
    stop = info.get("sl")

    if side not in ("LONG", "SHORT"):
        return None

    multiplier = specs.get("contract_multiplier")

    if not all(
        _finite(value)
        for value in (qty, price, stop, multiplier)
    ):
        return None

    if qty <= 0 or price <= 0 or stop <= 0 or multiplier <= 0:
        return None

    if side == "LONG" and stop >= price:
        return None

    if side == "SHORT" and stop <= price:
        return None

    open_fee = _positive_config(config, "open_taker_fee")
    close_fee = _positive_config(config, "close_taker_fee")
    slippage = _positive_config(config, "slippage_pct")

    if any(x is None for x in (open_fee, close_fee, slippage)):
        return None

    gross_risk = qty * abs(price - stop) * multiplier
    notional = qty * price * multiplier

    costs = notional * (
        open_fee + close_fee + 2.0 * slippage
    )

    result = gross_risk + costs

    return result if _finite(result) and result > 0 else None


def _portfolio_risk_used_usd(
    state_v2,
    config,
    exclude_symbol=None,
):
    """
    Sum all open position risks and all pending risk reservations.

    Invalid or incomplete active state returns None.
    It must never silently become zero risk.
    """

    symbols = state_v2.get("symbols")

    if not isinstance(symbols, dict) or not symbols:
        return None

    equity = state_v2.get("equity")

    if not _finite(equity) or equity <= 0:
        return None

    total = 0.0

    for symbol, symbol_state in symbols.items():
        if symbol == exclude_symbol:
            continue

        if not isinstance(symbol_state, dict):
            return None

        position = symbol_state.get("position")
        pending = symbol_state.get("pending_entry")

        if position is not None and pending is not None:
            return None

        if position is not None:
            risk = _position_risk_usd(position, config)

            if risk is None:
                return None

            total += risk

        elif pending is not None:
            risk = _pending_risk_reservation_usd(
                pending,
                equity,
                config,
            )

            if risk is None:
                return None

            total += risk

        if not _finite(total):
            return None

    return total


def _portfolio_risk_allows_pending(
    state_v2,
    symbol,
    signal,
    config,
):
    """
    Pre-signal risk gate.

    The candidate signal is not yet in state. Other active positions
    and pending entries are included.
    """

    limit = _portfolio_risk_limit(state_v2)

    if limit is None:
        return False, "invalid_equity_or_portfolio_risk_limit"

    equity = state_v2.get("equity")

    used = _portfolio_risk_used_usd(
        state_v2,
        config,
        exclude_symbol=symbol,
    )

    if used is None:
        return False, "existing_portfolio_risk_invalid"

    candidate = {
        "side": signal.get("signal"),
        "close_at_signal": signal.get("close"),
        "atr_at_signal": signal.get("atr_at_signal"),
    }

    reserve = _pending_risk_reservation_usd(
        candidate,
        equity,
        config,
    )

    if reserve is None:
        return False, "pending_risk_reservation_invalid"

    if used + reserve > limit + 1e-9:
        return False, "portfolio_risk_limit_exceeded"

    return True, None


def _portfolio_risk_allows_entry(
    state_v2,
    symbol,
    info,
    specs,
    config,
):
    """
    Execution-time risk gate.

    Excludes the current symbol's pending entry because it is being
    replaced by the actual entry risk in this same decision.
    """

    limit = _portfolio_risk_limit(state_v2)

    if limit is None:
        return False, "invalid_equity_or_portfolio_risk_limit"

    used = _portfolio_risk_used_usd(
        state_v2,
        config,
        exclude_symbol=symbol,
    )

    if used is None:
        return False, "existing_portfolio_risk_invalid"

    new_risk = _new_entry_risk_usd(
        info,
        specs,
        config,
    )

    if new_risk is None:
        return False, "new_entry_risk_invalid"

    if used + new_risk > limit + 1e-9:
        return False, "portfolio_risk_limit_exceeded"

    return True, None


# ============================================================
# CIRCUIT BREAKER
# ============================================================

def _sync_circuit_breaker(state_v2, was_active, events):
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


# ============================================================
# WHITELIST
# ============================================================

def load_allowed_symbols(path):
    path = Path(path)

    if not path.exists():
        raise RuntimeError(f"Whitelist file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read whitelist: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Whitelist root must be a JSON object")

    symbols = data.get("symbols")

    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError("Whitelist must contain a non-empty list")

    result = set()

    for symbol in symbols:
        if (
            not isinstance(symbol, str)
            or not re.fullmatch(r"[A-Z0-9]+-SWAP-USDT", symbol)
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


# ============================================================
# MAIN V2 PROCESSOR
# ============================================================

def process_symbol_candle(
    state_v2,
    symbol,
    klines,
    indicators,
    i,
    specs,
    config,
    signal=None,
    execution_callback=None,
    exit_callback=None,
    allowed_symbols=None,
):
    if not isinstance(state_v2, dict):
        raise ValueError("state_v2 must be a dictionary")

    symbols = state_v2.get("symbols")

    if not isinstance(symbols, dict):
        raise ValueError("V2 state has no valid symbols dictionary")

    if symbol not in symbols:
        raise ValueError(f"Symbol {symbol!r} not in V2 state")

    if (
        allowed_symbols is not None
        and symbol not in allowed_symbols
    ):
        raise ValueError(
            f"Symbol {symbol!r} not in allowed_symbols"
        )

    sym_state = symbols[symbol]

    if not isinstance(sym_state, dict):
        raise ValueError(f"Invalid state for symbol {symbol!r}")

    was_cb_active = bool(
        state_v2.get("circuit_breaker_active", False)
    )

    blocked_events = []
    effective_signal = signal

    directional_signal = (
        isinstance(signal, dict)
        and signal.get("signal") in ("LONG", "SHORT")
    )

    if directional_signal:
        reason = None

        if not _portfolio_has_room_for_pending(
            state_v2,
            symbol,
        ):
            reason = "portfolio_capacity_or_symbol_reserved"

        else:
            allowed, reason = _portfolio_risk_allows_pending(
                state_v2,
                symbol,
                signal,
                config,
            )

            if not allowed and reason is None:
                reason = "portfolio_risk_blocked"

        if reason is not None:
            blocked_events.append({
                "type": "SIGNAL_BLOCKED_PORTFOLIO",
                "symbol": symbol,
                "reason": reason,
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
                    "V2 capacity gate blocked ENTRY for %s",
                    symbol,
                )

                return {
                    "accepted": False,
                    "reason": "portfolio_capacity_exceeded",
                    "gate": "portfolio_capacity",
                }

            risk_allowed, risk_reason = (
                _portfolio_risk_allows_entry(
                    state_v2,
                    symbol,
                    info,
                    specs,
                    config,
                )
            )

            if not risk_allowed:
                logger.warning(
                    "V2 risk gate blocked ENTRY for %s: %s",
                    symbol,
                    risk_reason,
                )

                return {
                    "accepted": False,
                    "reason": risk_reason,
                    "gate": "portfolio_risk",
                }

        if execution_callback is not None:
            # Preserve Decision objects and dictionaries unchanged.
            return execution_callback(info, state_ref)

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
