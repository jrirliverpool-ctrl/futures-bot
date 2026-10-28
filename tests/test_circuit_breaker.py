"""tests/test_circuit_breaker.py — Prove CB counters + activation work."""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from paper_engine import (
    close_position,
    process_candle,
    reset_period_losses_if_needed,
    _day_key,
    _week_key,
    DEFAULT_CONFIG,
    DEFAULT_STATE,
)


def _make_state(equity=1000.0):
    s = copy.deepcopy(DEFAULT_STATE)
    s["initial_equity"] = equity
    s["equity"] = equity
    s["peak_equity"] = equity
    s["daily_loss"] = 0.0
    s["weekly_loss"] = 0.0
    return s


def _make_pos(side="LONG", entry=100.0, size=10.0, sl=95.0, tp1=110.0,
              realized_pnl=0.0, remaining=None):
    return {
        "side": side, "entry_price": entry, "entry_candle_ts": 0,
        "size": size, "remaining_size": size if remaining is None else remaining,
        "tp1_size": 0.0, "realized_pnl": realized_pnl,
        "initial_sl": sl, "current_sl": sl, "initial_tp1": tp1,
        "tp1_hit": False, "be_moved": False, "trailing_active": False,
        "highest_high_since_entry": entry, "lowest_low_since_entry": entry,
        "candles_since_entry": 0, "atr_at_entry": 5.0,
        "signal_candle_ts": 0, "commission_paid_usd": 0.0, "funding_pnl_usd": 0.0,
    }


def _specs():
    return {"contract_multiplier": 1.0, "min_qty": 0.01, "max_qty": 1000.0,
            "qty_step": 0.01, "min_notional": 0.0, "max_leverage": 10.0}


def _cfg():
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["slippage_pct"] = 0.0
    c["open_taker_fee"] = 0.0
    c["close_taker_fee"] = 0.0
    return c


# ============================================================
# close_position loss propagation
# ============================================================

def test_close_position_losing_increments_daily_loss():
    s = _make_state(1000.0)
    pos = _make_pos(side="LONG", entry=100.0, size=10.0)
    r = close_position(s, pos, exit_price=95.0, specs=_specs(), config=_cfg())
    assert r["pnl_net"] < 0
    assert s["daily_loss"] > 0
    assert abs(s["daily_loss"] - abs(r["pnl_net"])) < 1e-6


def test_close_position_losing_increments_weekly_loss():
    s = _make_state(1000.0)
    pos = _make_pos(side="LONG", entry=100.0, size=10.0)
    r = close_position(s, pos, exit_price=95.0, specs=_specs(), config=_cfg())
    assert s["weekly_loss"] > 0
    assert abs(s["weekly_loss"] - abs(r["pnl_net"])) < 1e-6


def test_close_position_winning_does_not_increment_gross_loss():
    """Gross-loss semantics: wins do NOT offset accumulated losses."""
    s = _make_state(1000.0)
    s["daily_loss"] = 10.0
    s["weekly_loss"] = 10.0
    pos = _make_pos(side="LONG", entry=100.0, size=10.0, tp1=110.0)
    r = close_position(s, pos, exit_price=110.0, specs=_specs(), config=_cfg())
    assert r["pnl_net"] > 0
    assert s["daily_loss"] == 10.0
    assert s["weekly_loss"] == 10.0


def test_close_position_accumulates_two_losses():
    s = _make_state(1000.0)
    c = _cfg()
    r1 = close_position(s, _make_pos(entry=100.0, size=10.0), 95.0, _specs(), c)
    r2 = close_position(s, _make_pos(entry=100.0, size=10.0), 95.0, _specs(), c)
    expected = abs(r1["pnl_net"]) + abs(r2["pnl_net"])
    assert abs(s["daily_loss"] - expected) < 1e-6
    assert abs(s["weekly_loss"] - expected) < 1e-6


def test_close_position_tp1_profit_then_net_loss():
    """TP1 realized +20, remaining leg loses -50 → pnl_net < 0."""
    s = _make_state(1000.0)
    pos = _make_pos(side="LONG", entry=100.0, size=10.0,
                    realized_pnl=20.0, remaining=5.0)
    r = close_position(s, pos, exit_price=90.0, specs=_specs(), config=_cfg())
    assert r["pnl_net"] < 0
    assert abs(s["daily_loss"] - abs(r["pnl_net"])) < 1e-6


# ============================================================
# Reset logic
# ============================================================

def test_reset_zeroes_on_new_day_and_week():
    s = _make_state(1000.0)
    s["daily_loss"] = 25.0
    s["weekly_loss"] = 25.0
    s["loss_day_key"] = 20000
    s["loss_week_key"] = 2900

    ts = 20100 * 86_400_000  # new day 20100, week 2871
    reset_period_losses_if_needed(s, ts)

    assert s["daily_loss"] == 0.0
    assert s["weekly_loss"] == 0.0
    assert s["loss_day_key"] == 20100
    assert s["loss_week_key"] == 2871


def test_reset_keeps_weekly_on_same_week():
    s = _make_state(1000.0)
    s["daily_loss"] = 25.0
    s["weekly_loss"] = 25.0
    s["loss_day_key"] = 20000
    s["loss_week_key"] = 2871  # same week as 20100

    ts = 20100 * 86_400_000
    reset_period_losses_if_needed(s, ts)

    assert s["daily_loss"] == 0.0  # new day
    assert s["weekly_loss"] == 25.0  # same week preserved


def test_reset_keeps_daily_on_same_day():
    s = _make_state(1000.0)
    s["daily_loss"] = 25.0
    s["weekly_loss"] = 25.0
    s["loss_day_key"] = 20100
    s["loss_week_key"] = 2871

    ts = 20100 * 86_400_000 + 3_600_000  # same day, +1h
    reset_period_losses_if_needed(s, ts)

    assert s["daily_loss"] == 25.0
    assert s["weekly_loss"] == 25.0


# ============================================================
# CB integration via process_candle
# ============================================================

def test_cb_fires_on_daily_loss():
    s = _make_state(1000.0)
    c = _cfg()
    c["daily_loss_limit"] = 0.03  # 30 on 1000
    s["position"] = _make_pos(side="LONG", entry=100.0, size=100.0, sl=95.0)
    candle = [1_000_000, "100", "101", "94", "96", "1000"]
    s_after, events = process_candle(s, [candle], {"atr": [5.0]}, 0, _specs(), c)

    assert s_after["circuit_breaker_active"] is True
    cb = [e for e in events if e["type"] == "CIRCUIT_BREAKER"]
    assert len(cb) == 1
    assert cb[0]["reason"] == "DAILY_LOSS"


def test_cb_does_not_fire_below_threshold():
    s = _make_state(1000.0)
    c = _cfg()
    c["daily_loss_limit"] = 0.03  # 30 on 1000
    s["position"] = _make_pos(side="LONG", entry=100.0, size=5.0, sl=95.0)
    candle = [1_000_000, "100", "101", "94", "96", "1000"]
    s_after, events = process_candle(s, [candle], {"atr": [5.0]}, 0, _specs(), c)

    assert s_after["circuit_breaker_active"] is False
    assert s_after["daily_loss"] > 0


def test_cb_fires_on_weekly_loss():
    s = _make_state(1000.0)
    c = _cfg()
    c["daily_loss_limit"] = 0.50
    c["weekly_loss_limit"] = 0.03  # 30 on 1000
    s["position"] = _make_pos(side="LONG", entry=100.0, size=10.0, sl=95.0)
    candle = [1_000_000, "100", "101", "94", "96", "1000"]
    s_after, events = process_candle(s, [candle], {"atr": [5.0]}, 0, _specs(), c)

    assert s_after["circuit_breaker_active"] is True
    cb = [e for e in events if e["type"] == "CIRCUIT_BREAKER"]
    assert len(cb) == 1
    assert cb[0]["reason"] == "WEEKLY_LOSS"


def test_cb_blocks_new_entry_after_firing():
    s = _make_state(1000.0)
    c = _cfg()
    c["daily_loss_limit"] = 0.01
    s["position"] = _make_pos(side="LONG", entry=100.0, size=10.0, sl=95.0)

    # Candle 1: SL exit → CB fires
    c1 = [1_000_000, "100", "101", "94", "96", "1000"]
    s, _ = process_candle(s, [c1], {"atr": [5.0]}, 0, _specs(), c)
    assert s["circuit_breaker_active"] is True

    # Candle 2: new signal → should NOT create pending_entry
    sig = {"signal": "LONG", "signal_candle_ts": 2_000_000,
           "atr_at_signal": 5.0, "close": 96.0}
    c2 = [2_000_000, "96", "99", "95", "98", "1000"]
    s, _ = process_candle(s, [c1, c2], {"atr": [5.0, 5.0]}, 1, _specs(), c, signal=sig)

    assert s["pending_entry"] is None