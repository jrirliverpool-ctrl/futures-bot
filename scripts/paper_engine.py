"""
paper_engine.py — ApexQuant V1 Paper Trading Engine.
v3.6 — Equity-based loss, strict validation, TP1 realized PnL booked to equity,
no double-count at close.
"""
import copy
import math
from decimal import Decimal, ROUND_DOWN

def floor_to_step(value: float, step: float) -> float:
    if step <= 0: raise ValueError("step must be > 0")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    n = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(n * d_step)

def _is_finite_number(v) -> bool:
    if isinstance(v, bool): return False
    if not isinstance(v, (int, float)): return False
    return math.isfinite(v)

def apply_slippage(price: float, is_entry: bool, side: str, slip_pct: float) -> float:
    if slip_pct < 0: raise ValueError("slip_pct must be >= 0")
    if side == "LONG": return price * (1 + slip_pct) if is_entry else price * (1 - slip_pct)
    if side == "SHORT": return price * (1 - slip_pct) if is_entry else price * (1 + slip_pct)
    raise ValueError(f"Invalid side: {side}")

def _is_valid_atr(x) -> bool:
    if isinstance(x, bool): return False
    if not isinstance(x, (int, float)): return False
    if x != x: return False
    if x in (float("inf"), float("-inf")): return False
    if x <= 0: return False
    return True

def _resolve_atr(atr_current, atr_at_entry) -> float:
    if _is_valid_atr(atr_current): return float(atr_current)
    if _is_valid_atr(atr_at_entry): return float(atr_at_entry)
    raise ValueError(f"ATR unavailable: atr_current={atr_current!r}, atr_at_entry={atr_at_entry!r}")

def compute_initial_levels(entry_price, atr_at_signal, side, config):
    if not (_is_finite_number(entry_price) and _is_finite_number(atr_at_signal)):
        raise ValueError("entry_price and ATR must be finite numbers")
    if entry_price <= 0 or atr_at_signal <= 0:
        raise ValueError("entry_price and ATR must be > 0")
    sl_dist = config["sl_atr_mult"] * atr_at_signal
    tp_dist = config["tp1_atr_mult"] * atr_at_signal
    if side == "LONG": return entry_price - sl_dist, entry_price + tp_dist
    if side == "SHORT": return entry_price + sl_dist, entry_price - tp_dist
    raise ValueError(f"Invalid side: {side}")

def calculate_position_size(equity, entry_price, stop_loss, specs, risk_per_trade, leverage=None, leverage_cap=None):
    if not (_is_finite_number(equity) and _is_finite_number(entry_price) and 
            _is_finite_number(stop_loss) and _is_finite_number(risk_per_trade)):
        return None
        
    if equity <= 0 or entry_price <= 0 or stop_loss <= 0 or risk_per_trade <= 0:
        return None
        
    if leverage is not None:
        if isinstance(leverage, bool) or not isinstance(leverage, int): return None
        if leverage <= 0: return None
        max_lev = specs.get("max_leverage", float("inf"))
        if leverage > max_lev: return None
        if leverage_cap is not None and leverage > leverage_cap: return None
        
    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0: return None
    
    multiplier = specs.get("contract_multiplier")
    if not _is_finite_number(multiplier) or multiplier <= 0:
        return None
        
    qty_step = specs.get("qty_step")
    min_qty = specs.get("min_qty")
    max_qty = specs.get("max_qty")
    
    if not _is_finite_number(qty_step) or qty_step <= 0: return None
    if not _is_finite_number(min_qty) or min_qty < 0: return None
    if not _is_finite_number(max_qty) or max_qty <= 0: return None

    risk_dollars = equity * risk_per_trade
    qty_raw = risk_dollars / (stop_distance * multiplier)
    qty = floor_to_step(qty_raw, qty_step)

    if qty < min_qty: return None
    if qty > max_qty: qty = floor_to_step(max_qty, qty_step)
    if qty < min_qty: return None

    min_notional = specs.get("min_notional", 0)
    if not _is_finite_number(min_notional) or min_notional < 0: return None

    notional = qty * multiplier * entry_price
    if min_notional > 0 and notional < min_notional: return None

    return qty

def _day_key(ts_ms): return ts_ms // 86_400_000
def _week_key(ts_ms): return ts_ms // (7 * 86_400_000)

def reset_period_losses_if_needed(state, ts):
    current_day = _day_key(ts)
    current_week = _week_key(ts)
    if state.get("loss_day_key") != current_day:
        state["daily_start_equity"] = state["equity"]
        state["loss_day_key"] = current_day
    if state.get("loss_week_key") != current_week:
        state["weekly_start_equity"] = state["equity"]
        state["loss_week_key"] = current_week

def _refresh_period_loss(state):
    day_start = state.get("daily_start_equity")
    if day_start is None:
        day_start = state["initial_equity"]
        state["daily_start_equity"] = day_start
    week_start = state.get("weekly_start_equity")
    if week_start is None:
        week_start = state["initial_equity"]
        state["weekly_start_equity"] = week_start
    state["daily_loss"] = max(0.0, day_start - state["equity"])
    state["weekly_loss"] = max(0.0, week_start - state["equity"])

def update_peak_equity(state):
    state["peak_equity"] = max(state.get("peak_equity", state["initial_equity"]), state["equity"])

def current_total_drawdown(state):
    peak = state.get("peak_equity", state["initial_equity"])
    if peak <= 0: return 0.0
    return max(0.0, (peak - state["equity"]) / peak)

def execute_pending_entry(state, candle, specs, config, execution_callback=None):
    events = []
    pending = state.get("pending_entry")
    if pending is None: return state, events

    ts = int(candle[0])
    if config.get("kill_switch_active", False):
        events.append({"type": "KILL_SWITCH_BLOCKED", "stage": "entry_execution", "reason": config.get("kill_switch_reason"), "side": pending.get("side"), "timestamp": ts})
        state["pending_entry"] = None
        return state, events

    side = pending["side"]
    open_price = float(candle[1])
    if not math.isfinite(open_price) or open_price <= 0:
        events.append({"type": "ENTRY_REJECTED", "reason": "invalid_open_price", "timestamp": ts})
        state["pending_entry"] = None
        return state, events

    entry_price = apply_slippage(open_price, is_entry=True, side=side, slip_pct=config["slippage_pct"])
    sl, tp1 = compute_initial_levels(entry_price, pending["atr_at_signal"], side, config)

    qty = calculate_position_size(
        state["equity"], entry_price, sl, specs, config["risk_per_trade"],
        leverage=config.get("leverage"), leverage_cap=config.get("leverage_cap"),
    )

    if qty is None:
        events.append({"type": "ENTRY_REJECTED", "reason": "invalid_size", "timestamp": ts})
        state["pending_entry"] = None
        return state, events

    if execution_callback is not None:
        allowed = execution_callback({
            "intent": "ENTRY", "side": side, "qty": qty, "price": entry_price,
            "sl": sl, "tp1": tp1, "leverage": config.get("leverage", 1), "timestamp": ts,
            "signal_candle_ts": pending.get("signal_candle_ts"),
        })
        if not allowed:
            events.append({"type": "ENTRY_REJECTED", "reason": "execution_manager_rejected", "timestamp": ts})
            state["pending_entry"] = None
            return state, events

    multiplier = specs["contract_multiplier"]
    entry_notional = qty * entry_price * multiplier
    entry_commission = entry_notional * config.get("open_taker_fee", 0.0)
    state["equity"] -= entry_commission

    state["position"] = {
        "side": side, "entry_price": entry_price, "entry_candle_ts": ts,
        "size": qty, "remaining_size": qty, "tp1_size": 0.0,
        "realized_pnl": 0.0, "initial_sl": sl, "current_sl": sl, "initial_tp1": tp1,
        "tp1_hit": False, "be_moved": False, "trailing_active": False,
        "highest_high_since_entry": entry_price, "lowest_low_since_entry": entry_price,
        "candles_since_entry": 0, "atr_at_entry": pending["atr_at_signal"],
        "signal_candle_ts": pending["signal_candle_ts"],
        "commission_paid_usd": entry_commission, "funding_pnl_usd": 0.0,
    }
    state["pending_entry"] = None

    events.append({
        "type": "ENTRY", "side": side, "price": entry_price, "size": qty,
        "sl": sl, "tp1": tp1, "notional_usd": entry_notional,
        "commission_usd": entry_commission, "timestamp": ts,
    })
    return state, events

def manage_position_on_candle(pos, candle, atr_current, config, exit_callback=None, specs=None):
    side = pos["side"]
    h = float(candle[2])
    l = float(candle[3])
    c = float(candle[4])
    ts = int(candle[0])

    atr_current = _resolve_atr(atr_current, pos.get("atr_at_entry"))
    p = copy.deepcopy(pos)
    events = []

    sl_hit = (side == "LONG" and l <= p["current_sl"]) or (side == "SHORT" and h >= p["current_sl"])
    tp1_hit_this_candle = False
    if not p["tp1_hit"]:
        tp1_hit_this_candle = (side == "LONG" and h >= p["initial_tp1"]) or (side == "SHORT" and l <= p["initial_tp1"])

    if sl_hit:
        if exit_callback is None or exit_callback({
            "intent": "EXIT", "side": side, "qty": p["remaining_size"],
            "price": p["current_sl"], "timestamp": ts
        }):
            events.append({
                "type": "EXIT", "reason": "SL", "price": p["current_sl"], "timestamp": ts,
                "ambiguous": tp1_hit_this_candle, "remaining_size": p["remaining_size"],
            })
            return p, events, True
        else:
            return p, [{"type": "EXIT_REJECTED", "reason": "execution_manager_rejected", "timestamp": ts}], False

    if side == "LONG": p["highest_high_since_entry"] = max(p["highest_high_since_entry"], h)
    else: p["lowest_low_since_entry"] = min(p["lowest_low_since_entry"], l)

    if not p["be_moved"]:
        risk_distance = abs(p["entry_price"] - p["initial_sl"])
        r_mult = config["be_trigger_at_r"]
        be_trigger = p["entry_price"] + r_mult * risk_distance if side == "LONG" else p["entry_price"] - r_mult * risk_distance
        be_reached = (side == "LONG" and h >= be_trigger) or (side == "SHORT" and l <= be_trigger)
        if be_reached:
            offset = config["be_sl_offset_atr"] * p["atr_at_entry"]
            new_sl = p["entry_price"] + offset if side == "LONG" else p["entry_price"] - offset
            if side == "LONG": p["current_sl"] = max(p["current_sl"], new_sl)
            else: p["current_sl"] = min(p["current_sl"], new_sl)
            p["be_moved"] = True
            events.append({"type": "BE_MOVED", "new_sl": p["current_sl"], "timestamp": ts})

    if not p["tp1_hit"] and tp1_hit_this_candle:
        tp1_size = p["size"] * 0.5
        if exit_callback is None or exit_callback({
            "intent": "TP1_HIT", "side": side, "qty": tp1_size,
            "price": p["initial_tp1"], "timestamp": ts
        }):
            multiplier = specs.get("contract_multiplier") if isinstance(specs, dict) else None
            if not _is_finite_number(multiplier) or multiplier <= 0:
                raise ValueError("A valid positive contract_multiplier is required for TP1 accounting")

            dir_sign = 1 if side == "LONG" else -1
            pnl_tp1 = tp1_size * (p["initial_tp1"] - p["entry_price"]) * multiplier * dir_sign
            tp1_notional = tp1_size * p["initial_tp1"] * multiplier
            tp1_commission = tp1_notional * config.get("close_taker_fee", 0.0)

            p["tp1_hit"] = True
            p["tp1_size"] = tp1_size
            p["remaining_size"] = p["size"] - tp1_size
            p["realized_pnl"] += pnl_tp1
            p["commission_paid_usd"] = p.get("commission_paid_usd", 0.0) + tp1_commission

            events.append({
                "type": "TP1_HIT", "side": side, "price": p["initial_tp1"], "portion": 0.5,
                "size": tp1_size, "remaining_size": p["remaining_size"],
                "entry_price": p["entry_price"], "sl": p["current_sl"], "tp1": p["initial_tp1"],
                "pnl_usd": pnl_tp1, "notional_usd": tp1_notional,
                "commission_usd": tp1_commission, "net_pnl_usd": pnl_tp1 - tp1_commission,
                "reason": None, "ambiguous_exit": False, "timestamp": ts, "exit_price_adj": None,
            })

    if p["tp1_hit"]:
        p["trailing_active"] = True
        trailing_distance = config["trailing_atr_mult"] * atr_current
        if side == "LONG":
            new_sl = p["highest_high_since_entry"] - trailing_distance
            p["current_sl"] = max(p["current_sl"], new_sl)
        else:
            new_sl = p["lowest_low_since_entry"] + trailing_distance
            p["current_sl"] = min(p["current_sl"], new_sl)

    p["candles_since_entry"] += 1
    if p["candles_since_entry"] >= config["time_exit_candles"]:
        if exit_callback is None or exit_callback({
            "intent": "EXIT", "side": side, "qty": p["remaining_size"],
            "price": c, "timestamp": ts
        }):
            events.append({
                "type": "EXIT", "reason": "TIME_EXIT", "price": c,
                "timestamp": ts, "ambiguous": False, "remaining_size": p["remaining_size"],
            })
            return p, events, True
        else:
            return p, [{"type": "EXIT_REJECTED", "reason": "execution_manager_rejected", "timestamp": ts}], False

    return p, events, False

def close_position(state, pos, exit_price, specs, config):
    side = pos["side"]
    entry = pos["entry_price"]
    remaining = pos["remaining_size"]
    multiplier = specs["contract_multiplier"]

    exit_price_adj = apply_slippage(exit_price, is_entry=False, side=side, slip_pct=config["slippage_pct"])
    dir_sign = 1 if side == "LONG" else -1
    pnl_remaining = remaining * (exit_price_adj - entry) * multiplier * dir_sign
    pnl_gross = pos.get("realized_pnl", 0.0) + pnl_remaining

    final_notional = remaining * exit_price_adj * multiplier
    final_commission = final_notional * config.get("close_taker_fee", 0.0)

    prior_commissions = pos.get("commission_paid_usd", 0.0)
    total_comm = prior_commissions + final_commission
    prior_funding = pos.get("funding_pnl_usd", 0.0)
    pnl_net = pnl_gross - total_comm + prior_funding

    pnl_for_equity = pnl_remaining - final_commission
    state["equity"] += pnl_for_equity
    update_peak_equity(state)

    return {
        "pnl_gross": pnl_gross, "final_commission": final_commission,
        "total_commission": total_comm, "exit_price_adj": exit_price_adj,
        "pnl_net": pnl_net,
    }

def apply_funding_if_due(state, candle, specs, config, events):
    current_ts = int(candle[0])
    pos = state.get("position")
    if not pos or pos.get("remaining_size", 0) <= 0: return
    if current_ts % 28_800_000 != 0: return

    funding_rates = config.get("funding_rates") or {}
    if not funding_rates: return
    if current_ts not in funding_rates: raise ValueError(f"Missing funding rate for timestamp {current_ts}")
    if state.get("last_funding_ts") == current_ts: return
    
    rate = funding_rates[current_ts]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)): raise ValueError("Invalid funding rate type")
    if rate != rate: raise ValueError("Funding rate is NaN")
    if rate == float("inf") or rate == float("-inf"): raise ValueError("Funding rate is infinite")

    size = pos["remaining_size"]
    open_price = float(candle[1])
    mult = specs["contract_multiplier"]
    notional = size * open_price * mult
    s = 1 if pos["side"] == "LONG" else -1
    cashflow = -s * notional * rate
    state["equity"] += cashflow
    update_peak_equity(state)
    state["last_funding_ts"] = current_ts
    pos["funding_pnl_usd"] = pos.get("funding_pnl_usd", 0.0) + cashflow
    events.append({"type": "FUNDING", "cashflow_usd": cashflow, "timestamp": current_ts})

def process_candle(state, klines, indicators, i, specs, config, signal=None, execution_callback=None, exit_callback=None):
    state = copy.deepcopy(state)
    events = []
    candle = klines[i]
    ts = int(candle[0])

    reset_period_losses_if_needed(state, ts)

    if state.get("position") is not None:
        apply_funding_if_due(state, candle, specs, config, events)

    if state.get("pending_entry") is not None:
        state, entry_events = execute_pending_entry(state, candle, specs, config, execution_callback=execution_callback)
        events.extend(entry_events)

    position_closed = False
    if state.get("position") is not None:
        atr_current = indicators["atr"][i]
        
        pos_after, pos_events, exited = manage_position_on_candle(
            state["position"], candle, atr_current, config, exit_callback=exit_callback, specs=specs
        )
        events.extend(pos_events)

        for ev in pos_events:
            if ev["type"] == "TP1_HIT":
                pnl_tp1 = ev.get("pnl_usd")
                commission_tp1 = ev.get("commission_usd")
                if not _is_finite_number(pnl_tp1) or not _is_finite_number(commission_tp1) or commission_tp1 < 0:
                    raise ValueError("Invalid TP1 PnL or commission in accounting event")
                state["equity"] += pnl_tp1 - commission_tp1
                update_peak_equity(state)

        if exited:
            exit_event = next(e for e in pos_events if e["type"] == "EXIT")
            result = close_position(state, pos_after, exit_event["price"], specs, config)

            exit_event.update({
                "pnl_usd": result["pnl_gross"],
                "pnl_gross": result["pnl_gross"],
                "commission_usd": result["final_commission"],
                "total_commission_usd": result["total_commission"],
                "trade_net_pnl_usd": result["pnl_net"],
                "exit_price_adj": result["exit_price_adj"],
                "entry_price": pos_after["entry_price"],
            })
            state["position"] = None
            position_closed = True
            state["cooldown_until_index"] = i + config["cooldown_candles"] + 1
        else:
            state["position"] = pos_after

    _refresh_period_loss(state)

    if not state.get("circuit_breaker_active", False):
        if state["daily_loss"] >= state["initial_equity"] * config["daily_loss_limit"]:
            state["circuit_breaker_active"] = True
            events.append({"type": "CIRCUIT_BREAKER", "reason": "DAILY_LOSS", "equity": state["equity"], "timestamp": ts})
        elif state["weekly_loss"] >= state["initial_equity"] * config["weekly_loss_limit"]:
            state["circuit_breaker_active"] = True
            events.append({"type": "CIRCUIT_BREAKER", "reason": "WEEKLY_LOSS", "equity": state["equity"], "timestamp": ts})
        elif current_total_drawdown(state) >= config["total_dd_limit"]:
            state["circuit_breaker_active"] = True
            events.append({"type": "CIRCUIT_BREAKER", "reason": "TOTAL_DD", "equity": state["equity"], "peak_equity": state["peak_equity"], "drawdown": current_total_drawdown(state), "timestamp": ts})

    ks_active = config.get("kill_switch_active", False)
    if (signal is not None and state.get("position") is None and state.get("pending_entry") is None and not position_closed and not state.get("circuit_breaker_active")):
        if ks_active:
            if signal.get("signal") in ("LONG", "SHORT"):
                events.append({"type": "KILL_SWITCH_BLOCKED", "stage": "signal_acceptance", "reason": config.get("kill_switch_reason"), "side": signal["signal"], "timestamp": ts})
        else:
            cooldown_until = state.get("cooldown_until_index")
            if cooldown_until is not None and i < cooldown_until:
                events.append({"type": "COOLDOWN_SKIP", "until_index": cooldown_until, "timestamp": ts})
            elif signal.get("signal") in ("LONG", "SHORT"):
                state["pending_entry"] = {
                    "side": signal["signal"], "signal_candle_ts": signal["signal_candle_ts"],
                    "atr_at_signal": signal["atr_at_signal"], "close_at_signal": signal["close"],
                }
                events.append({"type": "SIGNAL", "signal": signal["signal"], "reason": signal.get("reason"), "timestamp": ts})

    state["last_processed_candle_ts"] = ts
    return state, events

DEFAULT_CONFIG = {
    "risk_per_trade": 0.01, "sl_atr_mult": 1.5, "tp1_atr_mult": 2.0, "be_trigger_at_r": 1.0,
    "trailing_atr_mult": 1.5, "be_sl_offset_atr": 0.1, "time_exit_candles": 48, "cooldown_candles": 2,
    "daily_loss_limit": 0.03, "weekly_loss_limit": 0.07, "total_dd_limit": 0.15,
    "slippage_pct": 0.0005, "open_taker_fee": 0.0, "close_taker_fee": 0.0, "interval_ms": 3_600_000,
    "kill_switch_active": False, "kill_switch_reason": None,
}

DEFAULT_STATE = {
    "version": "1.0.0", "initial_equity": 1000.0, "equity": 1000.0, "peak_equity": 1000.0,
    "last_processed_candle_ts": None, "position": None, "pending_entry": None, "cooldown_until_index": None,
    "daily_loss": 0.0, "weekly_loss": 0.0, "loss_day_key": None, "loss_week_key": None,
    "daily_start_equity": None, "weekly_start_equity": None, "circuit_breaker_active": False, "last_funding_ts": None,
}