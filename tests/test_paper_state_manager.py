import sys, os, copy
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from paper_engine import (
    floor_to_step, apply_slippage, compute_initial_levels,
    calculate_position_size, manage_position_on_candle,
    execute_pending_entry, process_candle, run_engine,
    reset_period_losses_if_needed, DEFAULT_CONFIG, DEFAULT_STATE,
)

SPECS = {
    "contract_multiplier": 0.001, "min_qty": 0.0001, "max_qty": 120.0,
    "qty_step": 0.0001, "min_notional": 0.0, "tick_size": 0.1, "max_leverage": 200.0,
}

def _candle(ts, o, h, l, c): return [ts, str(o), str(h), str(l), str(c), "1000"]
def _mk_state(): return copy.deepcopy(DEFAULT_STATE)
def _cfg():
    c = dict(DEFAULT_CONFIG)
    c["contract_multiplier_for_pnl"] = SPECS["contract_multiplier"]
    return c

def _make_position(side="LONG", entry=85000.0, sl=84250.0, tp1=86000.0, atr=500.0, size=1.0):
    return {
        "side": side, "entry_price": entry, "entry_candle_ts": 0,
        "size": size, "remaining_size": size, "tp1_size": 0.0,
        "realized_pnl": 0.0, "initial_sl": sl, "current_sl": sl, "initial_tp1": tp1,
        "tp1_hit": False, "be_moved": False, "trailing_active": False,
        "highest_high_since_entry": entry, "lowest_low_since_entry": entry,
        "candles_since_entry": 0, "atr_at_entry": atr, "signal_candle_ts": 0,
    }

def _cb_yes(info, state): return True
def _cb_no(info, state): return False

def test_floor_to_step():
    assert floor_to_step(13.333333, 0.0001) == 13.3333
    assert floor_to_step(0.00009, 0.0001) == 0.0

def test_apply_slippage():
    assert abs(apply_slippage(100.0, True, "LONG", 0.001) - 100.1) < 1e-9
    assert abs(apply_slippage(100.0, False, "SHORT", 0.001) - 100.1) < 1e-9

def test_compute_initial_levels():
    sl, tp1 = compute_initial_levels(100.0, 10.0, "LONG", DEFAULT_CONFIG)
    assert abs(sl - 85.0) < 1e-9
    assert abs(tp1 - 120.0) < 1e-9

def test_position_size_valid():
    qty = calculate_position_size(1000.0, 85000.0, 84250.0, SPECS, 0.01)
    assert qty is not None and abs(qty - 13.3333) < 1e-6

def test_position_size_below_min():
    qty = calculate_position_size(0.001, 85000.0, 84250.0, SPECS, 0.01)
    assert qty is None

def test_sl_hit_long():
    pos = _make_position()
    candle = _candle(1000, 85000, 85100, 84000, 84500)
    _, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is True
    assert events[0]["reason"] == "SL"

def test_be_moved():
    pos = _make_position()
    candle = _candle(1000, 85000, 85800, 84800, 85700)
    pos_after, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is False
    assert pos_after["be_moved"] is True

def test_tp1_reduces_remaining_size():
    pos = _make_position(size=10.0)
    candle = _candle(1000, 85000, 86200, 84900, 86100)
    pos_after, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg(), specs=SPECS)
    assert exited is False
    assert pos_after["tp1_hit"] is True
    assert abs(pos_after["remaining_size"] - 5.0) < 1e-9

def test_time_exit():
    pos = _make_position()
    pos["candles_since_entry"] = 47
    candle = _candle(1000, 85000, 85100, 84900, 85050)
    _, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is True
    assert events[0]["reason"] == "TIME_EXIT"

def test_pending_entry_executed():
    state = _mk_state()
    state["pending_entry"] = {"side": "LONG", "signal_candle_ts": 1000, "atr_at_signal": 500.0, "close_at_signal": 85000.0}
    candle = _candle(3600000, 85100, 85300, 85000, 85200)
    new_state, events = execute_pending_entry(state, candle, SPECS, _cfg(), execution_callback=_cb_yes)
    assert new_state["position"] is not None

def test_daily_breaker():
    state = _mk_state()
    state["daily_loss"] = 30.0
    state["loss_day_key"] = 0
    klines = [_candle(1000, 100, 101, 99, 100)]
    new_state, events = process_candle(state, klines, {"atr": [1.0]}, 0, SPECS, _cfg())
    assert new_state["circuit_breaker_active"] is True

def test_halt_trading_blocks_signal():
    state = _mk_state()
    state["halt_trading"] = True
    klines = [_candle(1000, 100, 101, 99, 100)]
    sig = {"signal": "LONG", "signal_candle_ts": 1000, "atr_at_signal": 1.0, "close": 100.0}
    new_state, events = process_candle(state, klines, {"atr": [1.0]}, 0, SPECS, _cfg(), signal=sig)
    assert new_state["pending_entry"] is None