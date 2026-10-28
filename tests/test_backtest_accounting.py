"""tests/test_backtest_accounting.py — Focused accounting tests for backtest.py.

Verifies the four v2.3 fixes:
  1. compute_mtm_equity does NOT double-count TP1
  2. compute_metrics prefers trade_net_pnl_usd over pnl_usd
  3. forced-close emits trade_net_pnl_usd
  4. forced-close updates both mtm_curve[-1] and realized_curve[-1]
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

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
        "equity": 1005.0,  # e.g. initial 1000 + TP1 net +5
        "position": {
            "side": "LONG", "entry_price": 100.0,
            "remaining_size": 5.0,  # half remaining after TP1
            "realized_pnl": 10.0,   # gross TP1 PnL
            "tp1_hit": True,
        },
    }
    # Unrealized = 5 * (110 - 100) * 1.0 = 50
    expected = 1005.0 + 50.0  # = 1055
    # OLD buggy code would return 1005 + 10 + 50 = 1065 (double-counted +10)
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
    # Short: price drops → profit. Unrealized = 10 * (95-100) * -1 = 50
    assert compute_mtm_equity(state, close_price=95.0, specs=_specs()) == 1050.0


# ============================================================
# Bug 2: compute_metrics prefers trade_net_pnl_usd
# ============================================================

def test_metrics_prefer_net_pnl_when_both_present():
    """If both pnl_usd (gross) and trade_net_pnl_usd (net) exist, use net."""
    events = [
        {"type": "EXIT", "pnl_usd": 100.0, "trade_net_pnl_usd": 90.0},
        {"type": "EXIT", "pnl_usd": -50.0, "trade_net_pnl_usd": -60.0},
    ]
    m = compute_metrics([1000.0] * 5, events, 1000.0)
    # If metrics used pnl_usd: PF = 100/50 = 2.0
    # If metrics used trade_net: PF = 90/60 = 1.5
    assert abs(m["profit_factor"] - 1.5) < 1e-6, (
        f"expected PF=1.5 (net), got {m['profit_factor']} "
        f"(gross would be 2.0)"
    )
    assert m["gross_profit"] == 90.0
    assert m["gross_loss"] == 60.0
    assert abs(m["expectancy"] - (90.0 - 60.0) / 2) < 1e-6


def test_metrics_fallback_to_gross_when_net_absent():
    """Synthetic tests without trade_net_pnl_usd still work (fallback to pnl_usd)."""
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
        {"type": "EXIT", "pnl_usd": 5.0, "trade_net_pnl_usd": -2.0},   # net loss
        {"type": "EXIT", "pnl_usd": 10.0, "trade_net_pnl_usd": 8.0},   # net win
    ]
    m = compute_metrics([1000.0] * 5, events, 1000.0)
    # Net: -2 (loss) and +8 (win) → win_rate = 1/2 = 0.5
    assert abs(m["win_rate"] - 0.5) < 1e-9
    assert m["wins"] == 1
    assert m["losses"] == 1


# ============================================================
# Bug 3: forced-close emits trade_net_pnl_usd
# ============================================================

def test_forced_close_event_has_net_pnl():
    """When a position is open at end-of-backtest, the BACKTEST_END EXIT
    must carry trade_net_pnl_usd so metrics can use net consistently."""
    # Build synthetic uptrend that causes a LONG entry and never exits
    # before the last candle.
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    from tests_helpers_synth import make_synth_klines  # type: ignore  # noqa

    # Fallback: build inline
    klines = []
    ts = 1_700_000_000_000
    price = 100.0
    for i in range(300):
        o = price
        h = price + 1.0
        l = price - 1.0
        c = price + 0.5
        klines.append([ts + i * 3_600_000, str(o), str(h), str(l), str(c), "1000"])
        price = c
    result = run_backtest(klines, _specs(), initial_equity=1000.0)
    exit_events = [e for e in result["events"] if e["type"] == "EXIT"]
    if not exit_events:
        pytest.skip("no exit occurred — synthetic data too benign")
    for e in exit_events:
        assert "trade_net_pnl_usd" in e, (
            f"EXIT event missing trade_net_pnl_usd: {e}"
        )
        assert "pnl_gross" in e
        assert "total_commission_usd" in e


# ============================================================
# Bug 4: forced-close updates BOTH curves
# ============================================================

def test_forced_close_updates_both_curves():
    """After forced close, mtm_curve[-1] == realized_curve[-1] == final equity."""
    klines = []
    ts = 1_700_000_000_000
    price = 100.0
    for i in range(300):
        o = price
        h = price + 1.0
        l = price - 1.0
        c = price + 0.5
        klines.append([ts + i * 3_600_000, str(o), str(h), str(l), str(c), "1000"])
        price = c

    result = run_backtest(klines, _specs(), initial_equity=1000.0)

    final_equity = result["final_state"]["equity"]
    mtm_last = result["equity_curve"][-1]
    realized_last = result["realized_equity_curve"][-1]

    # If a position was still open (position=None after forced close)
    # both should equal final_equity.
    assert result["final_state"]["position"] is None, (
        "final_state.position should be None (forced close)"
    )
    assert abs(mtm_last - final_equity) < 1e-6, (
        f"mtm_curve[-1]={mtm_last} != final_equity={final_equity} "
        f"(forced close did not update mtm_curve)"
    )
    assert abs(realized_last - final_equity) < 1e-6