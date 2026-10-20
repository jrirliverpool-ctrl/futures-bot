"""
paper_engine.py — ApexQuant V1 Paper Trading Engine.
v2.9 - Standardized Callback Payloads.
"""

import copy
from decimal import Decimal, ROUND_DOWN

# ... (بقیه توابع مثل floor_to_step, apply_slippage, ... بدون تغییر) ...

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
    entry_price = apply_slippage(open_price, is_entry=True, side=side, slip_pct=config["slippage_pct"])
    sl, tp1 = compute_initial_levels(entry_price, pending["atr_at_signal"], side, config)

    qty = calculate_position_size(
        state["equity"], entry_price, sl, specs, config["risk_per_trade"],
        leverage=config.get("leverage"), leverage_cap=config.get("leverage_cap"),
    )

    if qty is None:
        events.append({"type": "ENTRY_REJECTED", "reason": "invalid_qty", "timestamp": ts})
        state["pending_entry"] = None
        return state, events

    # ✅ v2.9: کلیدهای استاندارد برای ENTRY
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
        "commission_paid_usd": entry_commission,
    }
    state["pending_entry"] = None

    events.append({
        "type": "ENTRY", "side": side, "price": entry_price, "size": qty,
        "sl": sl, "tp1": tp1, "notional_usd": entry_notional,
        "commission_usd": entry_commission, "timestamp": ts,
    })
    return state, events

# ... (manage_position_on_candle و close_position بدون تغییر باقی می‌مانند، 
# فقط مطمئن شوید که exit_callback در آن‌ها از کلید "price" استفاده می‌کند) ...