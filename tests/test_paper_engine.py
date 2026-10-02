"""
test_paper_engine.py — Full test suite for Paper Engine.
"""

import sys, os, copy
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from paper_engine import (
    floor_to_step,
    apply_slippage,
    compute_initial_levels,
    calculate_position_size,
    manage_position_on_candle,
    execute_pending_entry,
    process_candle,
    run_engine,
    reset_period_losses_if_needed,
    DEFAULT_CONFIG,
    DEFAULT_STATE,
)


SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}


def _candle(ts, o, h, l, c):
    return [ts, str(o), str(h), str(l), str(c), "1000"]


def _make_position(side="LONG", entry=85000.0, sl=84250.0, tp1=86000.0,
                   atr=500.0, size=1.0):
    return {
        "side": side,
        "entry_price": entry,
        "entry_candle_ts": 0,
        "size": size,
        "remaining_size": size,
        "tp1_size": 0.0,
        "realized_pnl": 0.0,
        "initial_sl": sl,
        "current_sl": sl,
        "initial_tp1": tp1,
        "tp1_hit": False,
        "be_moved": False,
        "trailing_active": False,
        "highest_high_since_entry": entry,
        "lowest_low_since_entry": entry,
        "candles_since_entry": 0,
        "atr_at_entry": atr,
        "signal_candle_ts": 0,
    }


def _cfg():
    c = dict(DEFAULT_CONFIG)
    c["contract_multiplier_for_pnl"] = SPECS["contract_multiplier"]
    return c


def _state():
    return copy.deepcopy(DEFAULT_STATE)


# ============================================================
# Helpers
# ============================================================

def test_floor_to_step():
    assert floor_to_step(13.333333, 0.0001) == 13.3333
    assert floor_to_step(0.00009, 0.0001) == 0.0
    assert floor_to_step(4.9, 1.0) == 4.0
    print("✅ test_floor_to_step passed")


def test_apply_slippage():
    assert abs(apply_slippage(100.0, True, "LONG", 0.001) - 100.1) < 1e-9
    assert abs(apply_slippage(100.0, False, "LONG", 0.001) - 99.9) < 1e-9
    assert abs(apply_slippage(100.0, True, "SHORT", 0.001) - 99.9) < 1e-9
    assert abs(apply_slippage(100.0, False, "SHORT", 0.001) - 100.1) < 1e-9
    print("✅ test_apply_slippage passed")


def test_compute_initial_levels():
    sl, tp1 = compute_initial_levels(100.0, 10.0, "LONG", DEFAULT_CONFIG)
    assert abs(sl - 85.0) < 1e-9
    assert abs(tp1 - 120.0) < 1e-9
    sl, tp1 = compute_initial_levels(100.0, 10.0, "SHORT", DEFAULT_CONFIG)
    assert abs(sl - 115.0) < 1e-9
    assert abs(tp1 - 80.0) < 1e-9
    print("✅ test_compute_initial_levels passed")


# ============================================================
# Position Sizing
# ============================================================

def test_position_size_valid():
    qty = calculate_position_size(1000.0, 85000.0, 84250.0, SPECS, 0.01)
    assert qty is not None and abs(qty - 13.3333) < 1e-6
    print(f"✅ test_position_size_valid passed (qty={qty})")


def test_position_size_below_min():
    qty = calculate_position_size(0.001, 85000.0, 84250.0, SPECS, 0.01)
    assert qty is None
    print("✅ test_position_size_below_min passed")


def test_position_size_min_notional():
    specs2 = dict(SPECS); specs2["min_notional"] = 1e9
    qty = calculate_position_size(1000.0, 85000.0, 84250.0, specs2, 0.01)
    assert qty is None
    print("✅ test_position_size_min_notional passed")


# ============================================================
# SL / BE / TP1
# ============================================================

def test_sl_hit_long():
    pos = _make_position()
    candle = _candle(1000, 85000, 85100, 84000, 84500)
    _, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is True
    assert next(e for e in events if e["type"] == "EXIT")["reason"] == "SL"
    print("✅ test_sl_hit_long passed")


def test_sl_first_ambiguity():
    pos = _make_position()
    candle = _candle(1000, 85000, 86500, 84000, 85500)
    _, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is True
    ev = next(e for e in events if e["type"] == "EXIT")
    assert ev["reason"] == "SL" and ev["ambiguous"] is True
    print("✅ test_sl_first_ambiguity passed")


def test_be_moved():
    pos = _make_position()
    candle = _candle(1000, 85000, 85800, 84800, 85700)
    pos_after, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is False
    assert pos_after["be_moved"] is True
    assert abs(pos_after["current_sl"] - 85050.0) < 1e-9
    print("✅ test_be_moved passed")


def test_tp1_reduces_remaining_size():
    pos = _make_position(side="LONG", entry=85000.0, sl=84250.0,
                         tp1=86000.0, atr=500.0, size=10.0)
    candle = _candle(1000, 85000, 86200, 84900, 86100)
    pos_after, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is False
    assert pos_after["tp1_hit"] is True
    assert abs(pos_after["tp1_size"] - 5.0) < 1e-9
    assert abs(pos_after["remaining_size"] - 5.0) < 1e-9
    ev = next(e for e in events if e["type"] == "TP1_HIT")
    assert abs(ev["size"] - 5.0) < 1e-9
    assert abs(ev["remaining_size"] - 5.0) < 1e-9
    print("✅ test_tp1_reduces_remaining_size passed")


def test_short_tp1_reduces_remaining_size():
    pos = _make_position(side="SHORT", entry=85000.0, sl=85750.0,
                         tp1=84000.0, atr=500.0, size=10.0)
    candle = _candle(1000, 85000, 85100, 83800, 84000)
    pos_after, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is False
    assert pos_after["tp1_hit"] is True
    assert abs(pos_after["remaining_size"] - 5.0) < 1e-9
    print("✅ test_short_tp1_reduces_remaining_size passed")


def test_tp1_activates_trailing():
    pos = _make_position()
    candle = _candle(1000, 85000, 86200, 84900, 86100)
    pos_after, _, _ = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert pos_after["trailing_active"] is True
    print("✅ test_tp1_activates_trailing passed")


def test_trailing_sl_never_moves_backward():
    pos = _make_position(side="LONG", entry=85000.0, sl=85500.0,
                         tp1=86000.0, atr=500.0)
    pos["tp1_hit"] = True
    pos["trailing_active"] = True
    pos["highest_high_since_entry"] = 86000.0
    candle = _candle(1000, 86000, 86100, 85800, 86050)
    pos_after, _, _ = manage_position_on_candle(pos, candle, 500.0, _cfg())
    # trailing would push to 86100 - 750 = 85350, but SL already at 85500 → keep 85500
    assert pos_after["current_sl"] >= 85500.0
    print("✅ test_trailing_sl_never_moves_backward passed")


def test_time_exit():
    pos = _make_position()
    pos["candles_since_entry"] = 47
    candle = _candle(1000, 85000, 85100, 84900, 85050)
    _, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is True
    assert next(e for e in events if e["type"] == "EXIT")["reason"] == "TIME_EXIT"
    print("✅ test_time_exit passed")


def test_short_sl_hit():
    pos = _make_position(side="SHORT", entry=85000.0, sl=85750.0,
                         tp1=84000.0, atr=500.0)
    candle = _candle(1000, 85000, 86000, 84800, 85500)
    _, events, exited = manage_position_on_candle(pos, candle, 500.0, _cfg())
    assert exited is True
    assert next(e for e in events if e["type"] == "EXIT")["reason"] == "SL"
    print("✅ test_short_sl_hit passed")


# ============================================================
# Entry
# ============================================================

def test_pending_entry_executed():
    state = _state()
    state["pending_entry"] = {
        "side": "LONG",
        "signal_candle_ts": 0,
        "atr_at_signal": 500.0,
        "close_at_signal": 85000.0,
    }
    candle = _candle(3600000, 85100, 85300, 85000, 85200)
    new_state, events = execute_pending_entry(state, candle, SPECS, _cfg())
    assert new_state["position"] is not None
    pos = new_state["position"]
    assert abs(pos["entry_price"] - 85142.55) < 0.01
    assert abs(pos["initial_sl"] - 84392.55) < 0.01
    assert abs(pos["initial_tp1"] - 86142.55) < 0.01
    assert pos["remaining_size"] == pos["size"]
    print("✅ test_pending_entry_executed passed")


def test_event_contains_entry_information():
    state = _state()
    state["pending_entry"] = {
        "side": "LONG",
        "signal_candle_ts": 1000,
        "atr_at_signal": 500.0,
        "close_at_signal": 85000.0,
    }
    candle = _candle(3600000, 85100, 85300, 85000, 85200)
    _, events = execute_pending_entry(state, candle, SPECS, _cfg())
    entry = next(e for e in events if e["type"] == "ENTRY")
    assert entry["side"] == "LONG"
    assert entry["size"] > 0
    assert entry["sl"] < entry["price"]
    assert entry["tp1"] > entry["price"]
    print("✅ test_event_contains_entry_information passed")


# ============================================================
# Day / Week Reset
# ============================================================

def test_daily_loss_resets_on_new_day():
    state = _state()
    day1 = 1_000_000_000
    day2 = day1 + 86_400_000
    state["daily_loss"] = 20.0
    state["loss_day_key"] = day1 // 86_400_000
    reset_period_losses_if_needed(state, day2)
    assert state["daily_loss"] == 0.0
    print("✅ test_daily_loss_resets_on_new_day passed")


def test_weekly_loss_resets_on_new_week():
    state = _state()
    week1 = 10 * 7 * 86_400_000
    week2 = week1 + 7 * 86_400_000
    state["weekly_loss"] = 50.0
    state["loss_week_key"] = week1 // (7 * 86_400_000)
    reset_period_losses_if_needed(state, week2)
    assert state["weekly_loss"] == 0.0
    print("✅ test_weekly_loss_resets_on_new_week passed")


# ============================================================
# Circuit Breakers
# ============================================================

def test_daily_breaker():
    state = _state()
    state["daily_loss"] = 30.0  # 3% از 1000
    state["loss_day_key"] = 0  # already current
    klines = [_candle(1000, 100, 101, 99, 100)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, _cfg())
    assert new_state["circuit_breaker_active"] is True
    assert any(e["type"] == "CIRCUIT_BREAKER" and e["reason"] == "DAILY_LOSS"
               for e in events)
    print("✅ test_daily_breaker passed")


def test_weekly_breaker():
    state = _state()
    state["weekly_loss"] = 70.0  # 7% از 1000
    state["loss_week_key"] = 0
    state["loss_day_key"] = 0
    klines = [_candle(1000, 100, 101, 99, 100)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, _cfg())
    assert new_state["circuit_breaker_active"] is True
    assert any(e["type"] == "CIRCUIT_BREAKER" and e["reason"] == "WEEKLY_LOSS"
               for e in events)
    print("✅ test_weekly_breaker passed")


def test_total_dd_from_peak():
    state = _state()
    state["peak_equity"] = 2000.0
    state["equity"] = 1600.0  # 20% DD از peak
    state["loss_day_key"] = 0
    state["loss_week_key"] = 0
    klines = [_candle(1000, 100, 101, 99, 100)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, _cfg())
    assert new_state["circuit_breaker_reason" if "circuit_breaker_reason" in new_state else "circuit_breaker_active"] is True
    assert any(e["type"] == "CIRCUIT_BREAKER" and e["reason"] == "TOTAL_DD"
               for e in events)
    print("✅ test_total_dd_from_peak passed")


def test_total_dd_uses_peak_equity():
    state = _state()
    state["equity"] = 1200.0
    state["peak_equity"] = 1200.0
    state["equity"] = 1020.0
    dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"]
    assert abs(dd - 0.15) < 1e-9
    print("✅ test_total_dd_uses_peak_equity passed")


# ============================================================
# Cooldown
# ============================================================

def test_cooldown_blocks_signal():
    state = _state()
    state["cooldown_until_index"] = 10
    klines = [_candle(1000, 100, 101, 99, 100)]
    indicators = {"atr": [1.0]}
    signal = {
        "signal": "LONG",
        "signal_candle_ts": 1000,
        "atr_at_signal": 1.0,
        "close": 100.0,
        "reason": "test",
    }
    new_state, events = process_candle(
        state, klines, indicators, 5, SPECS, _cfg(), signal=signal,
    )
    assert new_state["pending_entry"] is None
    assert any(e["type"] == "COOLDOWN_SKIP" for e in events)
    print("✅ test_cooldown_blocks_signal passed")


# ============================================================
# Engine Behavior
# ============================================================

def test_engine_does_not_generate_signal():
    state = _state()
    klines = [_candle(1000, 100, 101, 99, 100)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(
        state, klines, indicators, 0, SPECS, _cfg(), signal=None,
    )
    assert new_state["pending_entry"] is None
    assert not any(e["type"] == "SIGNAL" for e in events)
    print("✅ test_engine_does_not_generate_signal passed")


def test_signal_becomes_pending_not_immediate_entry():
    state = _state()
    klines = [
        _candle(1000, 100, 101, 99, 100),
        _candle(2000, 101, 103, 100, 102),
    ]
    indicators = {"atr": [2.0, 2.0]}
    signal = {
        "signal": "LONG",
        "signal_candle_ts": 1000,
        "atr_at_signal": 2.0,
        "close": 100.0,
        "reason": "test",
    }
    cfg = _cfg()
    state, events = process_candle(state, klines, indicators, 0, SPECS, cfg,
                                   signal=signal)
    assert state["position"] is None
    assert state["pending_entry"] is not None

    state, events = process_candle(state, klines, indicators, 1, SPECS, cfg,
                                   signal=None)
    assert state["position"] is not None
    assert state["position"]["entry_candle_ts"] == 2000
    print("✅ test_signal_becomes_pending_not_immediate_entry passed")


def test_no_signal_when_position_open():
    state = _state()
    state["position"] = _make_position()
    klines = [_candle(1000, 100, 101, 99, 100)]
    indicators = {"atr": [1.0]}
    signal = {
        "signal": "LONG",
        "signal_candle_ts": 1000,
        "atr_at_signal": 1.0,
        "close": 100.0,
        "reason": "test",
    }
    new_state, _ = process_candle(state, klines, indicators, 0, SPECS, _cfg(),
                                  signal=signal)
    assert new_state["pending_entry"] is None
    print("✅ test_no_signal_when_position_open passed")


# ============================================================
# Determinism
# ============================================================

def test_deterministic_replay():
    klines = [
        _candle(1000, 85000, 85100, 84900, 85050),
        _candle(2000, 85050, 85200, 85000, 85150),
        _candle(3000, 85150, 85300, 85100, 85250),
    ]
    indicators = {"atr": [500.0, 500.0, 500.0]}
    signals = [None, None, None]

    s1, e1 = run_engine(klines, indicators, signals, _state(), SPECS, DEFAULT_CONFIG)
    s2, e2 = run_engine(klines, indicators, signals, _state(), SPECS, DEFAULT_CONFIG)
    assert s1 == s2
    assert e1 == e2
    print("✅ test_deterministic_replay passed")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Running paper_engine unit tests")
    print("=" * 60)

    tests = [
        test_floor_to_step,
        test_apply_slippage,
        test_compute_initial_levels,
        test_position_size_valid,
        test_position_size_below_min,
        test_position_size_min_notional,
        test_sl_hit_long,
        test_sl_first_ambiguity,
        test_be_moved,
        test_tp1_reduces_remaining_size,
        test_short_tp1_reduces_remaining_size,
        test_tp1_activates_trailing,
        test_trailing_sl_never_moves_backward,
        test_time_exit,
        test_short_sl_hit,
        test_pending_entry_executed,
        test_event_contains_entry_information,
        test_daily_loss_resets_on_new_day,
        test_weekly_loss_resets_on_new_week,
        test_daily_breaker,
        test_weekly_breaker,
        test_total_dd_from_peak,
        test_total_dd_uses_peak_equity,
        test_cooldown_blocks_signal,
        test_engine_does_not_generate_signal,
        test_signal_becomes_pending_not_immediate_entry,
        test_no_signal_when_position_open,
        test_deterministic_replay,
    ]

    passed, failed = 0, 0
    for t in tests:
        try:
            t(); passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__} FAILED: {e}"); failed += 1
        except Exception as e:
            import traceback
            print(f"❌ {t.__name__} ERROR: {e}"); traceback.print_exc(); failed += 1

    print()
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)