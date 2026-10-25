"""
test_accounting.py — End-to-end accounting validation for paper_engine.
"""
import os
import sys
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from paper_engine import (
    process_candle,
    apply_funding_if_due,
    calculate_position_size,
    DEFAULT_CONFIG,
    DEFAULT_STATE,
)

SPECS = {
    "contract_multiplier": 1.0,
    "min_qty": 0.01,
    "max_qty": 1000.0,
    "qty_step": 0.01,
    "min_notional": 0.0,
    "tick_size": 0.01,
    "max_leverage": 10.0,
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


# 1. Full lifecycle & TP1 double-count prevention
def test_full_trade_lifecycle():
    state = _make_state()
    cfg = _make_cfg(fees=0.001)
    state["pending_entry"] = {"side": "LONG", "atr_at_signal": 5.0, "signal_candle_ts": TS_ENTRY - 3_600_000, "close_at_signal": 100.0}
    
    candle1 = _make_candle(TS_ENTRY, 100, 112, 99, 105)
    state, events = process_candle(state, [candle1], {"atr": [5.0]}, 0, SPECS, cfg)
    
    assert state["position"] is not None and state["position"]["tp1_hit"] is True
    pos = copy.deepcopy(state["position"])
    size, entry, tp1_size = pos["size"], pos["entry_price"], pos["size"] * 0.5
    entry_comm = size * entry * 0.001
    tp1_comm = tp1_size * 110.0 * 0.001
    
    # TP1 profit must be booked to equity
    tp1_profit = tp1_size * (110.0 - entry) * SPECS["contract_multiplier"]
    expected_equity_after_tp1 = 1000.0 - entry_comm + tp1_profit - tp1_comm
    assert abs(state["equity"] - expected_equity_after_tp1) < 1e-6

    state["position"]["current_sl"] = 115.0
    candle2 = _make_candle(TS_NEXT, 120, 125, 110, 120)
    state, events = process_candle(state, [candle2], {"atr": [5.0]}, 0, SPECS, cfg)
    
    assert state["position"] is None
    exit_event = next(e for e in events if e["type"] == "EXIT")
    
    # Trade net PnL must reflect the full trade (TP1 + remaining leg)
    gross = pos["realized_pnl"] + (size - tp1_size) * (115.0 - entry) * 1.0
    final_comm = (size - tp1_size) * 115.0 * 0.001
    total_comm = entry_comm + tp1_comm + final_comm
    assert abs(exit_event["trade_net_pnl_usd"] - (gross - total_comm)) < 1e-6
    
    # Equity must equal initial equity + total trade net PnL (no double count)
    assert abs(state["equity"] - (1000.0 + gross - total_comm)) < 1e-6


# 2. Funding accounting (LONG pays, SHORT receives)
def test_funding_long_pays():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    state["position"] = _long_position(size=10.0)
    apply_funding_if_due(state, _make_candle(TS_FUNDING, 100, 101, 99, 101), SPECS, cfg, [])
    assert abs(state["equity"] - 990.0) < 1e-6


# 3. Position sizing safety (reject zero/negative/invalid prices)
def test_invalid_entry_price_to_position_size():
    for bad in [float("nan"), float("inf"), 0.0, -100.0]:
        assert calculate_position_size(1000.0, bad, 95.0, SPECS, 0.01) is None


# 4. Idempotency of funding
def test_funding_idempotency():
    state = _make_state()
    cfg = _make_cfg(funding_rates={TS_FUNDING: 0.01})
    state["position"] = _long_position(size=10.0)
    candle = _make_candle(TS_FUNDING, 100, 101, 99, 101)
    e1 = []
    apply_funding_if_due(state, candle, SPECS, cfg, e1)
    e2 = []
    apply_funding_if_due(state, candle, SPECS, cfg, e2)
    assert len(e1) == 1 and len(e2) == 0
    assert abs(state["equity"] - 990.0) < 1e-6