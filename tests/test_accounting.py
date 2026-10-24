"""
test_accounting.py — End-to-end accounting validation for paper_engine.
"""
import os
import sys
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from paper_engine import (
    process_candle, apply_funding_if_due,
    DEFAULT_CONFIG, DEFAULT_STATE,
)

SPECS = {
    "contract_multiplier": 1.0,
    "min_qty": 0.01, "max_qty": 1000.0, "qty_step": 0.01,
    "min_notional": 0.0, "tick_size": 0.01, "max_leverage": 10.0,
}

TS_ENTRY = 1_700_010_000_000
TS_NEXT = 1_700_013_600_000
TS_FUNDING = 1_700_006_400_000
TS_DAY2 = 1_700_100_000_000
TS_WEEK2 = 1_700_700_000_000


def _make_candle(ts, o, h, l, c):
    return [ts, str(o), str(h), str(l), str(c), "1000"]

def _make_state(equity=1000.0):
    state = copy.deepcopy(DEFAULT_STATE)
    state["initial_equity"] = equity
    state["equity"] = equity
    state["peak_equity"] = equity
    return state

def _make_cfg(fees=0.0, funding_rates=None, slippage=0.0):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["open_taker_fee"] = fees
    cfg["close_taker_fee"] = fees
    cfg["slippage_pct"] = slippage
    if funding_rates is not None:
        cfg["funding_rates"] = funding_rates
    return cfg

def _long_position(entry=100.0, size=10.0, sl=50.0, tp1=200.0):
    return {
        "side": "LONG", "entry_price": entry, "remaining_size": size,
        "size": size, "realized_pnl": 0.0, "current_sl": sl, "initial_sl": sl,
        "initial_tp1": tp1, "tp1_hit": False, "commission_paid_usd": 0.0,
        "funding_pnl_usd": 0.0, "candles_since_entry": 0, "atr_at_entry": 5.0,
        "highest_high_since_entry": entry, "lowest_low_since_entry": entry,
        "trailing_active": False, "be_moved": False,
    }


# 1. Full lifecycle
def test_full_trade_lifecycle():
    state = _make_state()
    cfg = _make_cfg(fees=0.001)
    state["pending_entry"] = {"side": "LONG", "atr_at_signal": 5.0,
        "signal_candle_ts": TS_ENTRY - 3_600_000, "close_at_signal": 100.0}
    candle1 = _make_candle(TS_ENTRY, 100, 112, 99, 105)
    state, events = process_candle(state, [candle1], {"atr": [5.0]}, 0, SPECS, cfg)
    assert state["position"] is not None and state["position"]["tp1_hit"] is True
    pos = copy.deepcopy(state["position"])
    size, entry, tp1_size = pos["size"], pos["entry_price"], pos["size"] * 0.5
    entry_comm = size * entry * 0.001
    tp1_comm = tp1_size * 110.0 * 0.001
    assert abs(state["equity"] - (1000.0 - entry_comm - tp1_comm)) < 1e-6
    state["position"]["current_sl"] = 115.0
    candle2 = _make_candle(TS_NEXT, 120, 125, 110, 120)
    state, events = process_candle(state, [candle2], {"atr": [5.0]}, 0, SPECS, cfg)
    assert state["position"] is None
    exit_event = next(e for e in events if e["type"] == "EXIT")
    gross = pos["realized_pnl"] + (size - tp1_size) * (115.0 - entry) * 1.0
    final_comm = (size - tp1_size) * 115.0 * 0.001
    total_comm = entry_comm + tp1_comm + final_comm
    assert abs(exit_event["trade_net_pnl_usd"] - (gross - total_comm)) < 1e-6


# 2-3. Funding LONG/SHORT signs
def test_funding_long_pays():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    state["position"] = _long_position(size=10.0)
    apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])
    assert abs(state["equity"] - 990.0) < 1e-6

def test_funding_short_receives():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    pos = _long_position(size=10.0)
    pos["side"] = "SHORT"
    pos["current_sl"], pos["initial_sl"] = 150.0, 150.0
    pos["initial_tp1"] = 50.0
    state["position"] = pos
    apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])
    assert abs(state["equity"] - 1010.0) < 1e-6


# 4. Funding LONG negative rate
def test_funding_long_negative_rate():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: -0.01})
    state["position"] = _long_position(size=10.0)
    apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])
    assert abs(state["equity"] - 1010.0) < 1e-6


# 5. Idempotency
def test_funding_idempotency():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    state["position"] = _long_position(size=10.0)
    candle = _make_candle(TS_FUNDING, 100, 101, 99, 101)
    e1 = []; apply_funding_if_due(state, candle, SPECS, cfg, e1)
    e2 = []; apply_funding_if_due(state, candle, SPECS, cfg, e2)
    assert len(e1) == 1 and len(e2) == 0
    assert abs(state["equity"] - 990.0) < 1e-6


# 6-9. Rate validations
def test_funding_missing_rate_raises():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING + 1: 0.01})
    state["position"] = _long_position(size=10.0)
    with pytest.raises(ValueError, match="Missing funding rate"):
        apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])

def test_funding_invalid_rate_type_raises():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: "0.01"})
    state["position"] = _long_position(size=10.0)
    with pytest.raises(ValueError, match="Invalid funding rate type"):
        apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])

def test_funding_nan_rate_raises():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: float("nan")})
    state["position"] = _long_position(size=10.0)
    with pytest.raises(ValueError, match="NaN"):
        apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])

def test_funding_inf_rate_raises():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: float("inf")})
    state["position"] = _long_position(size=10.0)
    with pytest.raises(ValueError, match="infinite"):
        apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])


# 10. Funding not on entry candle
def test_funding_not_applied_on_entry_candle():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    state["pending_entry"] = {"side": "LONG", "atr_at_signal": 5.0,
        "signal_candle_ts": TS_FUNDING - 3_600_000, "close_at_signal": 100.0}
    state, events = process_candle(state, [_make_candle(TS_FUNDING, 100, 101, 99, 101)],
                                    {"atr": [5.0]}, 0, SPECS, cfg)
    assert state["position"] is not None
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0


# 11-12. Funding in trade PnL
def test_funding_in_trade_net_pnl_long():
    state = _make_state()
    cfg = _make_cfg(fees=0.0, funding_rates={TS_FUNDING: 0.01})
    state["position"] = _long_position(size=10.0, sl=101.0)
    state, events = process_candle(state, [_make_candle(TS_FUNDING, 100, 106, 99, 105)],
                                    {"atr": [5.0]}, 0, SPECS, cfg)
    exit_event = next(e for e in events if e["type"] == "EXIT")
    assert abs(exit_event["trade_net_pnl_usd"] - 0.0) < 1e-6

def test_funding_in_trade_net_pnl_short():
    state = _make_state()
    cfg = _make_cfg(fees=0.0, funding_rates={TS_FUNDING: 0.01})
    pos = _long_position(size=10.0)
    pos["side"] = "SHORT"
    pos["current_sl"], pos["initial_sl"] = 100.0, 100.0
    pos["initial_tp1"] = 50.0
    state["position"] = pos
    state, events = process_candle(state, [_make_candle(TS_FUNDING, 100, 101, 99, 100)],
                                    {"atr": [5.0]}, 0, SPECS, cfg)
    exit_event = next(e for e in events if e["type"] == "EXIT")
    assert abs(exit_event["trade_net_pnl_usd"] - 10.0) < 1e-6


# 13. Funding + EXIT rejected
def test_funding_applied_then_exit_rejected():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    state["position"] = _long_position(size=10.0, sl=99.0)
    state, events = process_candle(state, [_make_candle(TS_FUNDING, 100, 102, 98, 101)],
                                    {"atr": [5.0]}, 0, SPECS, cfg,
                                    exit_callback=lambda info: info["intent"] != "EXIT")
    assert state["position"] is not None
    assert abs(state["equity"] - 990.0) < 1e-6


# 14. TP1 preserved on EXIT rejected
def test_tp1_preserved_when_exit_rejected():
    state = _make_state()
    cfg = _make_cfg(fees=0.001)
    cfg["time_exit_candles"] = 1
    state["position"] = _long_position(size=10.0, tp1=110.0, sl=50.0)
    state, events = process_candle(state, [_make_candle(TS_ENTRY, 100, 115, 99, 105)],
                                    {"atr": [5.0]}, 0, SPECS, cfg,
                                    exit_callback=lambda info: info["intent"] != "EXIT")
    tp1_events = [e for e in events if e["type"] == "TP1_HIT"]
    assert len(tp1_events) == 1
    assert state["position"]["tp1_hit"] is True


# 15. TP1 preserved, then EXIT accepted on next candle
def test_tp1_preserved_then_next_candle_exit_accepted():
    state = _make_state()
    cfg = _make_cfg(fees=0.001)
    cfg["time_exit_candles"] = 1
    state["position"] = _long_position(size=10.0, tp1=110.0, sl=50.0)
    # Candle 1: TP1 hit, EXIT rejected
    state, _ = process_candle(state, [_make_candle(TS_ENTRY, 100, 115, 99, 105)],
                              {"atr": [5.0]}, 0, SPECS, cfg,
                              exit_callback=lambda info: info["intent"] != "EXIT")
    assert state["position"]["tp1_hit"] is True
    initial_equity_after_tp1 = state["equity"]
    # Candle 2: SL hits, EXIT accepted
    state["position"]["current_sl"] = 101.0
    state, events = process_candle(state, [_make_candle(TS_NEXT, 101, 102, 100, 101)],
                                    {"atr": [5.0]}, 0, SPECS, cfg)
    assert state["position"] is None
    exit_event = next(e for e in events if e["type"] == "EXIT")
    assert "trade_net_pnl_usd" in exit_event


# 16. SL exit rejected then retried next candle
def test_sl_exit_rejected_then_retried():
    state = _make_state()
    cfg = _make_cfg(fees=0.0)
    state["position"] = _long_position(size=10.0, sl=95.0)
    # Candle 1: low=94 hits SL, but rejected
    state, events = process_candle(state, [_make_candle(TS_ENTRY, 100, 101, 94, 96)],
                                    {"atr": [5.0]}, 0, SPECS, cfg,
                                    exit_callback=lambda info: info["intent"] != "EXIT")
    assert state["position"] is not None
    assert len([e for e in events if e["type"] == "EXIT_REJECTED"]) == 1
    assert abs(state["equity"] - 1000.0) < 1e-6
    # Candle 2: low=93 hits SL again, accepted
    state, events = process_candle(state, [_make_candle(TS_NEXT, 95, 96, 93, 94)],
                                    {"atr": [5.0]}, 0, SPECS, cfg)
    assert state["position"] is None
    # Gross: 10 * (95 - 100) = -50
    assert abs(state["equity"] - 950.0) < 1e-6


# 17. Daily loss resets at day boundary
def test_daily_loss_resets_at_day_boundary():
    state = _make_state()
    state["equity"] = 900.0  # Simulate 100 loss
    state["daily_start_equity"] = 1000.0
    state["daily_loss"] = 100.0
    state["loss_day_key"] = TS_ENTRY // 86_400_000
    state["loss_week_key"] = TS_ENTRY // (7 * 86_400_000)
    cfg = _make_cfg()
    # Candle on next day
    state, _ = process_candle(state, [_make_candle(TS_DAY2, 100, 101, 99, 100)],
                              {"atr": [5.0]}, 0, SPECS, cfg)
    # Daily loss should reset because new day starts at equity=900
    assert abs(state["daily_loss"] - 0.0) < 1e-6
    assert abs(state["daily_start_equity"] - 900.0) < 1e-6


# 18. Weekly loss resets at week boundary
def test_weekly_loss_resets_at_week_boundary():
    state = _make_state()
    state["equity"] = 800.0
    state["weekly_start_equity"] = 1000.0
    state["weekly_loss"] = 200.0
    state["loss_day_key"] = TS_ENTRY // 86_400_000
    state["loss_week_key"] = TS_ENTRY // (7 * 86_400_000)
    cfg = _make_cfg()
    state, _ = process_candle(state, [_make_candle(TS_WEEK2, 100, 101, 99, 100)],
                              {"atr": [5.0]}, 0, SPECS, cfg)
    assert abs(state["weekly_loss"] - 0.0) < 1e-6


# 19. Peak equity updated after funding gain
def test_funding_positive_updates_peak_equity():
    state = _make_state()  # equity=1000, peak=1000
    cfg = _make_cfg(funding_rates={TS_FUNDING: -0.01})  # LONG receives
    state["position"] = _long_position(size=10.0)
    apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])
    assert abs(state["equity"] - 1010.0) < 1e-6
    assert abs(state["peak_equity"] - 1010.0) < 1e-6


# 20. Invalid input to position sizing
def test_invalid_equity_to_position_size():
    from paper_engine import calculate_position_size
    for bad in [float("nan"), float("inf"), -float("inf"), -100.0, True]:
        assert calculate_position_size(bad, 100.0, 95.0, SPECS, 0.01) is None

def test_invalid_entry_price_to_position_size():
    from paper_engine import calculate_position_size
    for bad in [float("nan"), float("inf"), 0.0, -100.0]:
        assert calculate_position_size(1000.0, bad, 95.0, SPECS, 0.01) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])