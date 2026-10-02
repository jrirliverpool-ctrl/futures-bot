"""
paper_engine.py — ApexQuant V1 Paper Trading Engine.

Responsibilities:
  - Execute pending signal at next candle OPEN
  - Position sizing using real symbol specs
  - SL / BE / TP1 / trailing / time exit
  - Daily / weekly / total drawdown circuit breakers
  - Candle-based cooldown
  - Conservative SL-first ambiguity
  - Deterministic event logging

Non-responsibilities:
  - Signal generation (comes from outside)
  - Indicator calculation
  - Live order submission

Pure Python. No pandas/numpy.
"""

import copy
from decimal import Decimal, ROUND_DOWN


# ============================================================
# Helpers
# ============================================================

def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step must be > 0")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    n = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(n * d_step)


def apply_slippage(price: float, is_entry: bool, side: str, slip_pct: float) -> float:
    if slip_pct < 0:
        raise ValueError("slip_pct must be >= 0")
    if side == "LONG":
        return price * (1 + slip_pct) if is_entry else price * (1 - slip_pct)
    if side == "SHORT":
        return price * (1 - slip_pct) if is_entry else price * (1 + slip_pct)
    raise ValueError(f"Invalid side: {side}")


def compute_initial_levels(entry_price, atr_at_signal, side, config):
    if entry_price <= 0 or atr_at_signal <= 0:
        raise ValueError("entry_price and ATR must be > 0")
    sl_dist = config["sl_atr_mult"] * atr_at_signal
    tp_dist = config["tp1_atr_mult"] * atr_at_signal
    if side == "LONG":
        return entry_price - sl_dist, entry_price + tp_dist
    if side == "SHORT":
        return entry_price + sl_dist, entry_price - tp_dist
    raise ValueError(f"Invalid side: {side}")


def calculate_position_size(equity, entry_price, stop_loss, specs, risk_per_trade):
    if equity <= 0 or risk_per_trade <= 0:
        return None
    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        return None
    multiplier = specs["contract_multiplier"]
    if multiplier <= 0:
        return None
    risk_dollars = equity * risk_per_trade
    qty_raw = risk_dollars / (stop_distance * multiplier)
    qty = floor_to_step(qty_raw, specs["qty_step"])
    if qty < specs["min_qty"]:
        return None
    if qty > specs["max_qty"]:
        qty = floor_to_step(specs["max_qty"], specs["qty_step"])
    min_notional = specs.get("min_notional", 0)
    notional = qty * multiplier * entry_price
    if min_notional > 0 and notional < min_notional:
        return None
    return qty


# ============================================================
# Calendar / Risk Helpers
# ============================================================

def _day_key(ts_ms):
    return ts_ms // 86_400_000


def _week_key(ts_ms):
    return ts_ms // (7 * 86_400_000)


def reset_period_losses_if_needed(state, ts):
    current_day = _day_key(ts)
    current_week = _week_key(ts)
    if state.get("loss_day_key") != current_day:
        state["daily_loss"] = 0.0
        state["loss_day_key"] = current_day
    if state.get("loss_week_key") != current_week:
        state["weekly_loss"] = 0.0
        state["loss_week_key"] = current_week


def update_peak_equity(state):
    state["peak_equity"] = max(
        state.get("peak_equity", state["initial_equity"]),
        state["equity"],
    )


def current_total_drawdown(state):
    peak = state.get("peak_equity", state["initial_equity"])
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - state["equity"]) / peak)


# ============================================================
# Entry
# ============================================================

def execute_pending_entry(state, candle, specs, config):
    events = []
    pending = state.get("pending_entry")
    if pending is None:
        return state, events

    side = pending["side"]
    ts = int(candle[0])
    open_price = float(candle[1])

    entry_price = apply_slippage(
        open_price, is_entry=True, side=side, slip_pct=config["slippage_pct"],
    )

    sl, tp1 = compute_initial_levels(
        entry_price, pending["atr_at_signal"], side, config,
    )

    qty = calculate_position_size(
        state["equity"], entry_price, sl, specs, config["risk_per_trade"],
    )

    if qty is None:
        events.append({
            "type": "ENTRY_REJECTED",
            "reason": "invalid_or_below_min_position_size",
            "timestamp": ts,
        })
        state["pending_entry"] = None
        return state, events

    state["position"] = {
        "side": side,
        "entry_price": entry_price,
        "entry_candle_ts": ts,
        "size": qty,
        "remaining_size": qty,
        "tp1_size": 0.0,
        "realized_pnl": 0.0,
        "initial_sl": sl,
        "current_sl": sl,
        "initial_tp1": tp1,
        "tp1_hit": False,
        "be_moved": False,
        "trailing_active": False,
        "highest_high_since_entry": entry_price,
        "lowest_low_since_entry": entry_price,
        "candles_since_entry": 0,
        "atr_at_entry": pending["atr_at_signal"],
        "signal_candle_ts": pending["signal_candle_ts"],
    }
    state["pending_entry"] = None

    events.append({
        "type": "ENTRY",
        "side": side,
        "price": entry_price,
        "size": qty,
        "sl": sl,
        "tp1": tp1,
        "timestamp": ts,
    })
    return state, events


# ============================================================
# Position Management
# ============================================================

def manage_position_on_candle(pos, candle, atr_current, config):
    side = pos["side"]
    h = float(candle[2])
    l = float(candle[3])
    c = float(candle[4])
    ts = int(candle[0])

    if atr_current <= 0:
        atr_current = pos["atr_at_entry"]

    p = copy.deepcopy(pos)
    events = []

    # --------------------------------------------------------
    # 1. SL FIRST (conservative)
    # --------------------------------------------------------
    sl_hit = (
        (side == "LONG" and l <= p["current_sl"]) or
        (side == "SHORT" and h >= p["current_sl"])
    )

    tp1_hit_this_candle = False
    if not p["tp1_hit"]:
        tp1_hit_this_candle = (
            (side == "LONG" and h >= p["initial_tp1"]) or
            (side == "SHORT" and l <= p["initial_tp1"])
        )

    if sl_hit:
        events.append({
            "type": "EXIT",
            "reason": "SL",
            "price": p["current_sl"],
            "timestamp": ts,
            "ambiguous": tp1_hit_this_candle,
            "remaining_size": p["remaining_size"],
        })
        return p, events, True

    # --------------------------------------------------------
    # 2. Update excursion
    # --------------------------------------------------------
    if side == "LONG":
        p["highest_high_since_entry"] = max(p["highest_high_since_entry"], h)
    else:
        p["lowest_low_since_entry"] = min(p["lowest_low_since_entry"], l)

    # --------------------------------------------------------
    # 3. BE
    # --------------------------------------------------------
    if not p["be_moved"]:
        risk_distance = abs(p["entry_price"] - p["initial_sl"])
        be_trigger = (
            p["entry_price"] + risk_distance if side == "LONG"
            else p["entry_price"] - risk_distance
        )
        be_reached = (
            (side == "LONG" and h >= be_trigger) or
            (side == "SHORT" and l <= be_trigger)
        )
        if be_reached:
            offset = config["be_sl_offset_atr"] * p["atr_at_entry"]
            new_sl = (
                p["entry_price"] + offset if side == "LONG"
                else p["entry_price"] - offset
            )
            if side == "LONG":
                p["current_sl"] = max(p["current_sl"], new_sl)
            else:
                p["current_sl"] = min(p["current_sl"], new_sl)
            p["be_moved"] = True
            events.append({
                "type": "BE_MOVED",
                "new_sl": p["current_sl"],
                "timestamp": ts,
            })

    # --------------------------------------------------------
    # 4. TP1 = 50% realized
    # --------------------------------------------------------
    if not p["tp1_hit"] and tp1_hit_this_candle:
        tp1_size = p["size"] * 0.5
        dir_sign = 1 if side == "LONG" else -1
        pnl_tp1 = (
            tp1_size * (p["initial_tp1"] - p["entry_price"])
            * config["contract_multiplier_for_pnl"] * dir_sign
        )
        p["tp1_hit"] = True
        p["tp1_size"] = tp1_size
        p["remaining_size"] = p["size"] - tp1_size
        p["realized_pnl"] += pnl_tp1
        events.append({
            "type": "TP1_HIT",
            "price": p["initial_tp1"],
            "portion": 0.5,
            "size": tp1_size,
            "remaining_size": p["remaining_size"],
            "pnl_usd": pnl_tp1,
            "timestamp": ts,
        })

    # --------------------------------------------------------
    # 5. Trailing
    # --------------------------------------------------------
    if p["tp1_hit"]:
        p["trailing_active"] = True
        trailing_distance = config["trailing_atr_mult"] * atr_current
        if side == "LONG":
            new_sl = p["highest_high_since_entry"] - trailing_distance
            p["current_sl"] = max(p["current_sl"], new_sl)
        else:
            new_sl = p["lowest_low_since_entry"] + trailing_distance
            p["current_sl"] = min(p["current_sl"], new_sl)

    # --------------------------------------------------------
    # 6. Time Exit
    # --------------------------------------------------------
    p["candles_since_entry"] += 1
    if p["candles_since_entry"] >= config["time_exit_candles"]:
        events.append({
            "type": "EXIT",
            "reason": "TIME_EXIT",
            "price": c,
            "timestamp": ts,
            "ambiguous": False,
            "remaining_size": p["remaining_size"],
        })
        return p, events, True

    return p, events, False


# ============================================================
# Close Position
# ============================================================

def close_position(state, pos, exit_price, specs, config):
    side = pos["side"]
    entry = pos["entry_price"]
    remaining = pos["remaining_size"]
    multiplier = specs["contract_multiplier"]

    exit_price_adj = apply_slippage(
        exit_price, is_entry=False, side=side, slip_pct=config["slippage_pct"],
    )

    dir_sign = 1 if side == "LONG" else -1
    pnl_remaining = (
        remaining * (exit_price_adj - entry) * multiplier * dir_sign
    )
    pnl = pos.get("realized_pnl", 0.0) + pnl_remaining

    state["equity"] += pnl

    if pnl < 0:
        loss = abs(pnl)
        state["daily_loss"] = state.get("daily_loss", 0.0) + loss
        state["weekly_loss"] = state.get("weekly_loss", 0.0) + loss

    update_peak_equity(state)
    return pnl, exit_price_adj


# ============================================================
# Main Candle Processor
# ============================================================

def process_candle(state, klines, indicators, i, specs, config, signal=None):
    state = copy.deepcopy(state)
    events = []
    candle = klines[i]
    ts = int(candle[0])

    # Period reset
    reset_period_losses_if_needed(state, ts)

    # Entry from previous signal
    if state.get("pending_entry") is not None:
        state, entry_events = execute_pending_entry(state, candle, specs, config)
        events.extend(entry_events)

    # Position management
    position_closed = False
    if state.get("position") is not None:
        atr_current = indicators["atr"][i]
        pos_after, pos_events, exited = manage_position_on_candle(
            state["position"], candle, atr_current, config,
        )
        events.extend(pos_events)

        if exited:
            exit_event = next(e for e in pos_events if e["type"] == "EXIT")
            pnl, exit_price_adj = close_position(
                state, pos_after, exit_event["price"], specs, config,
            )
            exit_event["pnl_usd"] = pnl
            exit_event["exit_price_adj"] = exit_price_adj
            exit_event["entry_price"] = pos_after["entry_price"]
            state["position"] = None
            position_closed = True
            state["cooldown_until_index"] = i + config["cooldown_candles"] + 1

    # Circuit breakers
    if not state.get("circuit_breaker_active", False):
        if state["daily_loss"] >= state["initial_equity"] * config["daily_loss_limit"]:
            state["circuit_breaker_active"] = True
            events.append({
                "type": "CIRCUIT_BREAKER",
                "reason": "DAILY_LOSS",
                "equity": state["equity"],
                "timestamp": ts,
            })
        elif state["weekly_loss"] >= state["initial_equity"] * config["weekly_loss_limit"]:
            state["circuit_breaker_active"] = True
            events.append({
                "type": "CIRCUIT_BREAKER",
                "reason": "WEEKLY_LOSS",
                "equity": state["equity"],
                "timestamp": ts,
            })
        elif current_total_drawdown(state) >= config["total_dd_limit"]:
            state["circuit_breaker_active"] = True
            events.append({
                "type": "CIRCUIT_BREAKER",
                "reason": "TOTAL_DD",
                "equity": state["equity"],
                "peak_equity": state["peak_equity"],
                "drawdown": current_total_drawdown(state),
                "timestamp": ts,
            })

    # Signal ingestion
    if (signal is not None and
        state.get("position") is None and
        state.get("pending_entry") is None and
        not position_closed and
        not state.get("circuit_breaker_active")):

        cooldown_until = state.get("cooldown_until_index")
        if cooldown_until is not None and i < cooldown_until:
            events.append({
                "type": "COOLDOWN_SKIP",
                "until_index": cooldown_until,
                "timestamp": ts,
            })
        elif signal.get("signal") in ("LONG", "SHORT"):
            state["pending_entry"] = {
                "side": signal["signal"],
                "signal_candle_ts": signal["signal_candle_ts"],
                "atr_at_signal": signal["atr_at_signal"],
                "close_at_signal": signal["close"],
            }
            events.append({
                "type": "SIGNAL",
                "signal": signal["signal"],
                "reason": signal.get("reason"),
                "timestamp": ts,
            })

    state["last_processed_candle_ts"] = ts
    return state, events


# ============================================================
# Run Engine
# ============================================================

def run_engine(klines, indicators, signals, initial_state, specs, config, start_index=0):
    """
    signals: list indexed identically with klines. Strategy is external.
    Engine only consumes its output.
    """
    state = copy.deepcopy(initial_state)
    all_events = []
    config = copy.deepcopy(config)
    config["contract_multiplier_for_pnl"] = specs["contract_multiplier"]

    for i in range(start_index, len(klines)):
        signal = signals[i] if signals is not None and i < len(signals) else None
        state, events = process_candle(
            state, klines, indicators, i, specs, config, signal=signal,
        )
        all_events.extend(events)
    return state, all_events


# ============================================================
# Defaults
# ============================================================

DEFAULT_CONFIG = {
    "risk_per_trade": 0.01,
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 2.0,
    "trailing_atr_mult": 1.5,
    "be_sl_offset_atr": 0.1,
    "time_exit_candles": 48,
    "cooldown_candles": 2,
    "daily_loss_limit": 0.03,
    "weekly_loss_limit": 0.07,
    "total_dd_limit": 0.15,
    "slippage_pct": 0.0005,
    "interval_ms": 3_600_000,
}

DEFAULT_STATE = {
    "version": "1.0.0",
    "initial_equity": 1000.0,
    "equity": 1000.0,
    "peak_equity": 1000.0,
    "last_processed_candle_ts": None,
    "position": None,
    "pending_entry": None,
    "cooldown_until_index": None,
    "daily_loss": 0.0,
    "weekly_loss": 0.0,
    "loss_day_key": None,
    "loss_week_key": None,
    "circuit_breaker_active": False,
}