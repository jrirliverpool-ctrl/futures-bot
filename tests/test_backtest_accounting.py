"""tests/test_backtest_accounting.py — Focused accounting tests for backtest.py.

Verifies the four v2.3 fixes:
  1. compute_mtm_equity does NOT double-count TP1
  2. compute_metrics prefers trade_net_pnl_usd over pnl_usd
  3. forced-close emits trade_net_pnl_usd with reason=="BACKTEST_END"
  4. forced-close updates both mtm_curve[-1] and realized_curve[-1]

Design notes:
  - MTM tests use handcrafted state to isolate the formula.
  - Forced-close tests monkeypatch `backtest.process_candle` so we can
    GUARANTEE a position is open on the final candle. This avoids the
    fragile "synthetic data might trigger a signal" problem and avoids
    silent `pytest.skip`.
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backtest
from backtest import (
    compute_mtm_equity,
    compute_metrics,
    run_backtest,
)


def _specs():
    return {
        "contract_multiplier": 1.0,
        "min_qty": 0.01,
        "max_qty": 1000.0,
        "qty_step": 0.01,
        "min_notional": 0.0,
        "max_leverage": 10.0,
    }


def _flat_klines(n=300):
    """300 flat candles at price 100. No real signal triggers."""
    ts = 1_700_000_000_000
    return [
        [ts + i * 3_600_000, "100", "105", "95", "100", "1000"]
        for i in range(n)
    ]


def _position_dict(entry_price=100.0, size=10.0, sl=95.0, tp1=110.0):
    return {
        "side": "LONG",
        "entry_price": entry_price,
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
        "highest_high_since_entry": entry_price,
        "lowest_low_since_entry": entry_price,
        "candles_since_entry": 0,
        "atr_at_entry": 5.0,
        "signal_candle_ts": 0,
        "commission_paid_usd": 0.0,
        "funding_pnl_usd": 0.0,
    }


# ============================================================
# Bug 1: compute_mtm_equity must NOT add realized_pnl
# ============================================================

def test_mtm_no_double_count_when_no_position():
    state = {"equity": 1050.0, "position": None}
    assert compute_mtm_equity(state, close_price=100.0, specs=_specs()) == 1050.0


def test_mtm_no_double_count_before_tp1():
    """Position open, no TP1 yet. MTM = equity + unrealized."""
    state = {
        "equity": 1000.0,
        "position": {
            "side": "LONG", "entry_price": 100.0,
            "remaining_size": 10.0, "realized_pnl": 0.0,
        },
    }
    # Unrealized = 10 * (110 - 100) * 1.0 = 100
    assert compute_mtm_equity(state, close_price=110.0, specs=_specs()) == 1100.0


def test_mtm_does_not_double_count_tp1():
    """After TP1: equity already has TP1 net; must NOT add realized_pnl again."""
    state = {
        "equity": 1005.0,
        "position": {
            "side": "LONG", "entry_price": 100.0,
            "remaining_size": 5.0,
            "realized_pnl": 10.0,
            "tp1_hit": True,
        },
    }
    expected = 1005.0 + 50.0  # = 1055
    actual = compute_mtm_equity(state, close_price=110.0, specs=_specs())
    assert actual == expected, (
        f"MTM double-counts TP1: expected {expected}, got {actual}"
    )


def test_mtm_short_position_correct():
    state = {
        "equity": 1000.0,
        "position": {
            "side": "SHORT", "entry_price": 100.0,
            "remaining_size": 10.0, "realized_pnl": 0.0,
        },
    }
    # Short: price drops -> profit. Unrealized = 10 * (95-100) * -1 = 50
    assert compute_mtm_equity(state, close_price=95.0, specs=_specs()) == 1050.0


# ============================================================
# Bug 2: compute_metrics prefers trade_net_pnl_usd
# ============================================================

def test_metrics_prefer_net_pnl_when_both_present():
    events = [
        {"type": "EXIT", "pnl_usd": 100.0, "trade_net_pnl_usd": 90.0},
        {"type": "EXIT", "pnl_usd": -50.0, "trade_net_pnl_usd": -60.0},
    ]
    m = compute_metrics([1000.0] * 5, events, 1000.0)
    assert abs(m["profit_factor"] - 1.5) < 1e-6
    assert m["gross_profit"] == 90.0
    assert m["gross_loss"] == 60.0
    assert abs(m["expectancy"] - (90.0 - 60.0) / 2) < 1e-6


def test_metrics_fallback_to_gross_when_net_absent():
    events = [
        {"type": "EXIT", "pnl_usd": 100.0},
        {"type": "EXIT", "pnl_usd": -50.0},
    ]
    m = compute_metrics([1000.0] * 5, events, 1000.0)
    assert m["profit_factor"] == 2.0
    assert m["gross_profit"] == 100.0
    assert m["gross_loss"] == 50.0


def test_metrics_win_rate_uses_net():
    """A trade that is gross-positive but net-negative counts as a LOSS."""
    events = [
        {"type": "EXIT", "pnl_usd": 5.0, "trade_net_pnl_usd": -2.0},
        {"type": "EXIT", "pnl_usd": 10.0, "trade_net_pnl_usd": 8.0},
    ]
    m = compute_metrics([1000.0] * 5, events, 1000.0)
    assert abs(m["win_rate"] - 0.5) < 1e-9
    assert m["wins"] == 1
    assert m["losses"] == 1


# ============================================================
# Bug 3: forced-close emits trade_net_pnl_usd with reason=="BACKTEST_END"
# ============================================================

def test_forced_close_event_has_net_pnl_and_backtest_end_reason(monkeypatch):
    """Force a position on the last candle; run_backtest must emit exactly
    one EXIT with reason=="BACKTEST_END" and full accounting fields.

    We monkeypatch backtest.process_candle so the position is guaranteed
    to be open at the final candle — no silent skip, no fragile reliance
    on synthetic data triggering a real signal.
    """
    klines = _flat_klines(300)
    last_idx = len(klines) - 1

    def fake_process_candle(state, klines_arg, indicators, i, specs_arg,
                             config, signal=None,
                             execution_callback=None, exit_callback=None):
        state_copy = copy.deepcopy(state)
        if i == last_idx and state_copy.get("position") is None:
            state_copy["position"] = _position_dict()
        return state_copy, []

    monkeypatch.setattr(backtest, "process_candle", fake_process_candle)

    result = run_backtest(klines, _specs(), initial_equity=1000.0)

    exit_events = [e for e in result["events"] if e["type"] == "EXIT"]
    backtest_end = [e for e in exit_events if e.get("reason") == "BACKTEST_END"]

    assert len(backtest_end) == 1, (
        f"Expected exactly 1 BACKTEST_END exit; got {len(backtest_end)}. "
        f"All exit reasons: {[e.get('reason') for e in exit_events]}"
    )

    e = backtest_end[0]
    assert "trade_net_pnl_usd" in e and e["trade_net_pnl_usd"] is not None, (
        f"BACKTEST_END exit missing trade_net_pnl_usd: {e}"
    )
    assert "pnl_gross" in e and e["pnl_gross"] is not None
    assert "total_commission_usd" in e and e["total_commission_usd"] is not None
    assert "commission_usd" in e
    assert "exit_price_adj" in e


# ============================================================
# Bug 4: forced-close updates BOTH curves
# ============================================================

def test_forced_close_updates_both_curves(monkeypatch):
    """After forced close, mtm_curve[-1] == realized_curve[-1] == final equity."""
    klines = _flat_klines(300)
    last_idx = len(klines) - 1

    def fake_process_candle(state, klines_arg, indicators, i, specs_arg,
                             config, signal=None,
                             execution_callback=None, exit_callback=None):
        state_copy = copy.deepcopy(state)
        if i == last_idx and state_copy.get("position") is None:
            state_copy["position"] = _position_dict()
        return state_copy, []

    monkeypatch.setattr(backtest, "process_candle", fake_process_candle)

    result = run_backtest(klines, _specs(), initial_equity=1000.0)

    final_equity = result["final_state"]["equity"]
    mtm_last = result["equity_curve"][-1]
    realized_last = result["realized_equity_curve"][-1]

    assert result["final_state"]["position"] is None, (
        "final_state.position should be None after forced close"
    )
    assert abs(mtm_last - final_equity) < 1e-6, (
        f"mtm_curve[-1]={mtm_last} != final_equity={final_equity} "
        f"(forced close did not update mtm_curve)"
    )
    assert abs(realized_last - final_equity) < 1e-6, (
        f"realized_curve[-1]={realized_last} != final_equity={final_equity}"
    )