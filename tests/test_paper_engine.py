import sys, os, copy
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from paper_engine import process_candle, manage_position_on_candle, execute_pending_entry, calculate_position_size, DEFAULT_CONFIG, DEFAULT_STATE

SPECS = {"contract_multiplier": 0.001, "min_qty": 0.0001, "max_qty": 120.0, "qty_step": 0.0001, "min_notional": 0.0, "tick_size": 0.1, "max_leverage": 200.0}

def _candle(ts, o, h, l, c): return [ts, str(o), str(h), str(l), str(c), "1000"]
def _mk_state(): return copy.deepcopy(DEFAULT_STATE)
def _cfg():
    c = dict(DEFAULT_CONFIG)
    c["contract_multiplier_for_pnl"] = SPECS["contract_multiplier"]
    return c

def _cb_yes(info, state): return True
def _cb_no(info, state): return False

def test_position_size():
    qty = calculate_position_size(1000.0, 85000.0, 84250.0, SPECS, 0.01)
    assert qty == 13.3333

def test_trailing_sl_updated_for_next_candle():
    # تست می‌کند که Trailing SL در همان کندل باعث بسته شدن نشود (رفع باگ Look-ahead)
    pos = {"side": "LONG", "entry_price": 100.0, "initial_sl": 90.0, "current_sl": 90.0, "size": 10.0, "remaining_size": 10.0, "tp1_size": 0.0, "realized_pnl": 0.0, "tp1_hit": True, "be_moved": False, "trailing_active": True, "highest_high_since_entry": 120.0, "lowest_low_since_entry": 100.0, "candles_since_entry": 0, "atr_at_entry": 10.0}
    candle = _candle(1000, 100.0, 120.0, 90.0, 95.0) # Low لمس شده اما High بعدش اومده
    # در کد جدید، چون High آپدیت شده، SL جدید محاسبه میشه (مثلا 105)
    # اما چون Low=90 است، نباید در همین کندل ببندد (چون 90 < 105)
    pos_after, events, exited = manage_position_on_candle(pos, candle, 10.0, _cfg(), exit_callback=_cb_yes, specs=SPECS)
    
    # در کد قدیمی چون High اول آپدیت میشد و بعد Low چک میشد، فکر میکرد SL خورده و میبست. اما الان نمیبنده.
    assert exited is False 
    assert pos_after["current_sl"] > 90.0

def test_engine_pending_entry_with_callback():
    state = _mk_state()
    state["pending_entry"] = {"side": "LONG", "signal_candle_ts": 1000, "atr_at_signal": 500.0, "close_at_signal": 85000.0}
    candle = _candle(3600000, 85100, 85300, 85000, 85200)
    new_state, events = execute_pending_entry(state, candle, SPECS, _cfg(), execution_callback=_cb_yes)
    assert new_state["position"] is not None

def test_kill_switch_blocks_signal():
    state = _mk_state()
    klines = [_candle(1000, 100, 101, 99, 100)]
    cfg = _cfg()
    cfg["kill_switch_active"] = True
    cfg["kill_switch_reason"] = "test"
    sig = {"signal": "LONG", "signal_candle_ts": 1000, "atr_at_signal": 1.0, "close": 100.0}
    new_state, events = process_candle(state, klines, {"atr": [1.0]}, 0, SPECS, cfg, signal=sig)
    assert any(e["type"] == "KILL_SWITCH_BLOCKED" for e in events)

def test_halt_trading_blocks_signal():
    state = _mk_state()
    state["halt_trading"] = True
    klines = [_candle(1000, 100, 101, 99, 100)]
    sig = {"signal": "LONG", "signal_candle_ts": 1000, "atr_at_signal": 1.0, "close": 100.0}
    new_state, events = process_candle(state, klines, {"atr": [1.0]}, 0, SPECS, _cfg(), signal=sig)
    assert new_state["pending_entry"] is None