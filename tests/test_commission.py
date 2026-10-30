"""
test_commission.py — Commission tests.

Contract:
  - ENTRY commission is deducted from equity.
  - TP1_HIT commission is deducted from equity.
  - EXIT commission is recorded.
  - Gross and net PnL are reconciled.
  - Backtest metrics report commission cost.
  - Net return does not double-count commission.
"""

import copy
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from paper_engine import (
    floor_to_step,
    execute_pending_entry,
    process_candle,
    DEFAULT_CONFIG,
    DEFAULT_STATE,
)

try:
    from strategy_config import get_fee_config

    FEES = get_fee_config()

except (ImportError, AttributeError):
    FEES = {
        "open_taker_fee": 0.0005,
        "close_taker_fee": 0.0005,
        "open_maker_fee": 0.0002,
        "close_maker_fee": 0.0002,
        "source": "test-fallback",
    }

from compute_hash import compute_strategy_hash
from strategy_config import STRATEGY_CONFIG


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
    return [
        ts,
        str(o),
        str(h),
        str(l),
        str(c),
        "1000",
    ]


def _cfg():
    c = dict(DEFAULT_CONFIG)

    c["contract_multiplier_for_pnl"] = (
        SPECS["contract_multiplier"]
    )

    c["open_taker_fee"] = FEES["open_taker_fee"]
    c["close_taker_fee"] = FEES["close_taker_fee"]

    return c


def _mk_state():
    return copy.deepcopy(DEFAULT_STATE)


def _pending_entry(side="LONG"):
    return {
        "side": side,
        "signal_candle_ts": 1000,
        "atr_at_signal": 500.0,
        "close_at_signal": 85000.0,
    }


# ============================================================
# 1. Entry commission — LONG
# ============================================================

def test_entry_commission_long():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("LONG")

    initial_equity = state["equity"]

    candle = _candle(
        3600000,
        85100,
        85300,
        85000,
        85200,
    )

    new_state, events = execute_pending_entry(
        state,
        candle,
        SPECS,
        _cfg(),
    )

    entry_ev = next(
        e for e in events
        if e["type"] == "ENTRY"
    )

    assert "notional_usd" in entry_ev
    assert "commission_usd" in entry_ev

    expected_notional = (
        entry_ev["size"]
        * entry_ev["price"]
        * SPECS["contract_multiplier"]
    )

    expected_commission = (
        expected_notional
        * FEES["open_taker_fee"]
    )

    assert abs(
        entry_ev["notional_usd"] - expected_notional
    ) < 1e-6

    assert abs(
        entry_ev["commission_usd"] - expected_commission
    ) < 1e-9

    expected_equity = initial_equity - expected_commission

    assert abs(
        new_state["equity"] - expected_equity
    ) < 1e-9


# ============================================================
# 2. Entry commission — SHORT
# ============================================================

def test_entry_commission_short():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("SHORT")

    initial_equity = state["equity"]

    candle = _candle(
        3600000,
        85100,
        85200,
        84900,
        85000,
    )

    new_state, events = execute_pending_entry(
        state,
        candle,
        SPECS,
        _cfg(),
    )

    entry_ev = next(
        e for e in events
        if e["type"] == "ENTRY"
    )

    assert entry_ev["side"] == "SHORT"

    expected_notional = (
        entry_ev["size"]
        * entry_ev["price"]
        * SPECS["contract_multiplier"]
    )

    expected_commission = (
        expected_notional
        * FEES["open_taker_fee"]
    )

    assert abs(
        entry_ev["notional_usd"] - expected_notional
    ) < 1e-6

    assert abs(
        entry_ev["commission_usd"] - expected_commission
    ) < 1e-9

    assert abs(
        new_state["equity"]
        - (initial_equity - expected_commission)
    ) < 1e-9


# ============================================================
# 3. SL exit commission
# ============================================================

def test_sl_exit_commission():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("LONG")

    entry_candle = _candle(
        3600000,
        85100,
        85300,
        85000,
        85200,
    )

    state, entry_events = execute_pending_entry(
        state,
        entry_candle,
        SPECS,
        _cfg(),
    )

    entry_ev = next(
        e for e in entry_events
        if e["type"] == "ENTRY"
    )

    entry_commission = entry_ev["commission_usd"]
    size = entry_ev["size"]

    sl_candle = _candle(
        7200000,
        85000,
        85100,
        83000,
        83500,
    )

    indicators = {
        "atr": [500.0, 500.0]
    }

    new_state, events = process_candle(
        state,
        [entry_candle, sl_candle],
        indicators,
        1,
        SPECS,
        _cfg(),
        signal=None,
    )

    exit_ev = next(
        e for e in events
        if e["type"] == "EXIT"
    )

    assert exit_ev["reason"] == "SL"

    assert "commission_usd" in exit_ev
    assert "pnl_gross" in exit_ev
    assert "total_commission_usd" in exit_ev
    assert "trade_net_pnl_usd" in exit_ev

    exit_notional = (
        size
        * exit_ev["exit_price_adj"]
        * SPECS["contract_multiplier"]
    )

    expected_final_commission = (
        exit_notional
        * FEES["close_taker_fee"]
    )

    assert abs(
        exit_ev["commission_usd"] - expected_final_commission
    ) < 1e-6

    expected_total = (
        entry_commission + expected_final_commission
    )

    assert abs(
        exit_ev["total_commission_usd"] - expected_total
    ) < 1e-6

    expected_net = (
        exit_ev["pnl_gross"] - expected_total
    )

    assert abs(
        exit_ev["trade_net_pnl_usd"] - expected_net
    ) < 1e-6


# ============================================================
# 4. TP1 partial commission
# ============================================================

def test_tp1_partial_commission():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("LONG")

    entry_candle = _candle(
        3600000,
        85100,
        85300,
        85000,
        85200,
    )

    state, _ = execute_pending_entry(
        state,
        entry_candle,
        SPECS,
        _cfg(),
    )

    size = state["position"]["size"]

    tp1_candle = _candle(
        7200000,
        85500,
        86500,
        85200,
        86200,
    )

    indicators = {
        "atr": [500.0, 500.0]
    }

    new_state, events = process_candle(
        state,
        [entry_candle, tp1_candle],
        indicators,
        1,
        SPECS,
        _cfg(),
        signal=None,
    )

    assert new_state["position"] is not None

    tp1_ev = next(
        (
            e for e in events
            if e["type"] == "TP1_HIT"
        ),
        None,
    )

    assert tp1_ev is not None

    assert "commission_usd" in tp1_ev
    assert "notional_usd" in tp1_ev
    assert "net_pnl_usd" in tp1_ev

    tp1_size = tp1_ev["size"]

    expected_tp1_size = floor_to_step(
        size * 0.5,
        SPECS["qty_step"],
    )

    assert abs(
        tp1_size - expected_tp1_size
    ) < 1e-9

    expected_tp1_notional = (
        tp1_size
        * tp1_ev["price"]
        * SPECS["contract_multiplier"]
    )

    expected_tp1_commission = (
        expected_tp1_notional
        * FEES["close_taker_fee"]
    )

    assert abs(
        tp1_ev["notional_usd"] - expected_tp1_notional
    ) < 1e-6

    assert abs(
        tp1_ev["commission_usd"] - expected_tp1_commission
    ) < 1e-6

    expected_net = (
        tp1_ev["pnl_usd"] - expected_tp1_commission
    )

    assert abs(
        tp1_ev["net_pnl_usd"] - expected_net
    ) < 1e-6


# ============================================================
# 5. TP1 then SL — full sequence
# ============================================================

def test_tp1_then_sl_sequence():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("LONG")

    entry_candle = _candle(
        3600000,
        85100,
        85300,
        85000,
        85200,
    )

    state, entry_events = execute_pending_entry(
        state,
        entry_candle,
        SPECS,
        _cfg(),
    )

    entry_commission = next(
        e for e in entry_events
        if e["type"] == "ENTRY"
    )["commission_usd"]

    tp1_candle = _candle(
        7200000,
        85500,
        86500,
        85200,
        86200,
    )

    indicators_2 = {
        "atr": [500.0, 500.0]
    }

    state, tp1_events = process_candle(
        state,
        [entry_candle, tp1_candle],
        indicators_2,
        1,
        SPECS,
        _cfg(),
        signal=None,
    )

    tp1_ev = next(
        e for e in tp1_events
        if e["type"] == "TP1_HIT"
    )

    tp1_commission = tp1_ev["commission_usd"]

    sl_candle = _candle(
        10800000,
        85000,
        85100,
        83000,
        83500,
    )

    indicators_3 = {
        "atr": [500.0, 500.0, 500.0]
    }

    new_state, exit_events = process_candle(
        state,
        [entry_candle, tp1_candle, sl_candle],
        indicators_3,
        2,
        SPECS,
        _cfg(),
        signal=None,
    )

    exit_ev = next(
        e for e in exit_events
        if e["type"] == "EXIT"
    )

    final_commission = exit_ev["commission_usd"]

    assert exit_ev["reason"] == "SL"

    expected_total = (
        entry_commission
        + tp1_commission
        + final_commission
    )

    assert abs(
        exit_ev["total_commission_usd"] - expected_total
    ) < 1e-6

    expected_net = (
        exit_ev["pnl_gross"] - expected_total
    )

    assert abs(
        exit_ev["trade_net_pnl_usd"] - expected_net
    ) < 1e-6


# ============================================================
# 6. Time exit commission
# ============================================================

def test_time_exit_commission():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("LONG")

    entry_candle = _candle(
        3600000,
        85100,
        85300,
        85000,
        85200,
    )

    state, _ = execute_pending_entry(
        state,
        entry_candle,
        SPECS,
        _cfg(),
    )

    state["position"]["candles_since_entry"] = 47

    exit_candle = _candle(
        7200000,
        85500,
        85600,
        85000,
        85400,
    )

    indicators = {
        "atr": [500.0, 500.0]
    }

    new_state, events = process_candle(
        state,
        [entry_candle, exit_candle],
        indicators,
        1,
        SPECS,
        _cfg(),
        signal=None,
    )

    exit_ev = next(
        e for e in events
        if e["type"] == "EXIT"
    )

    assert exit_ev["reason"] == "TIME_EXIT"
    assert "commission_usd" in exit_ev
    assert exit_ev["commission_usd"] > 0


# ============================================================
# 7. Full equity reconciliation
# ============================================================

def test_equity_full_cycle():
    state = _mk_state()
    state["pending_entry"] = _pending_entry("LONG")

    initial_equity = state["equity"]

    entry_candle = _candle(
        3600000,
        85100,
        85300,
        85000,
        85200,
    )

    state, entry_events = execute_pending_entry(
        state,
        entry_candle,
        SPECS,
        _cfg(),
    )

    entry_commission = next(
        e for e in entry_events
        if e["type"] == "ENTRY"
    )["commission_usd"]

    sl_candle = _candle(
        7200000,
        85000,
        85100,
        83000,
        83500,
    )

    indicators = {
        "atr": [500.0, 500.0]
    }

    final_state, events = process_candle(
        state,
        [entry_candle, sl_candle],
        indicators,
        1,
        SPECS,
        _cfg(),
        signal=None,
    )

    exit_ev = next(
        e for e in events
        if e["type"] == "EXIT"
    )

    pnl_gross = exit_ev["pnl_gross"]

    total_comm = (
        entry_commission
        + exit_ev["commission_usd"]
    )

    expected_equity = (
        initial_equity
        + pnl_gross
        - total_comm
    )

    assert abs(
        final_state["equity"] - expected_equity
    ) < 1e-6


# ============================================================
# 8. Strategy hash unchanged by fee configuration access
# ============================================================

def test_strategy_hash_unchanged():
    before = compute_strategy_hash()

    _ = FEES

    after = compute_strategy_hash()

    assert after == before


def test_fee_config_not_in_strategy_config():
    forbidden = (
        "open_taker_fee",
        "close_taker_fee",
        "open_maker_fee",
        "close_maker_fee",
    )

    for key in forbidden:
        assert key not in STRATEGY_CONFIG, (
            f"{key} must not be in STRATEGY_CONFIG"
        )


# ============================================================
# 9. compute_metrics commission_cost_usd
# ============================================================

def test_metrics_commission_cost():
    from backtest import compute_metrics

    equity_curve = [
        995.0,
        1000.0,
        1005.0,
    ]

    events = [
        {
            "type": "ENTRY",
            "commission_usd": 0.50,
        },
        {
            "type": "TP1_HIT",
            "commission_usd": 0.25,
        },
        {
            "type": "EXIT",
            "commission_usd": 0.30,
            "pnl_usd": 3.0,
        },
    ]

    metrics = compute_metrics(
        equity_curve,
        events,
        initial_equity=1000.0,
        realized_final=1005.0,
    )

    assert "commission_cost_usd" in metrics

    expected = 0.50 + 0.25 + 0.30

    assert abs(
        metrics["commission_cost_usd"] - expected
    ) < 1e-9


# ============================================================
# 10. No double-count in net_return_pct
# ============================================================

def test_net_return_no_double_count():
    from backtest import compute_metrics

    initial = 1000.0
    mtm_final = 990.0

    events = [
        {
            "type": "ENTRY",
            "commission_usd": 5.0,
        },
        {
            "type": "EXIT",
            "commission_usd": 5.0,
            "pnl_usd": 0.0,
        },
    ]

    metrics = compute_metrics(
        [mtm_final],
        events,
        initial_equity=initial,
        realized_final=990.0,
    )

    expected_return = (
        (990.0 - 1000.0) / 1000.0
    ) * 100.0

    assert abs(
        metrics["net_return_pct"] - expected_return
    ) < 1e-9

    assert metrics["net_return_pct"] > -2.0


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_entry_commission_long,
        test_entry_commission_short,
        test_sl_exit_commission,
        test_tp1_partial_commission,
        test_tp1_then_sl_sequence,
        test_time_exit_commission,
        test_equity_full_cycle,
        test_strategy_hash_unchanged,
        test_fee_config_not_in_strategy_config,
        test_metrics_commission_cost,
        test_net_return_no_double_count,
    ]

    passed = 0
    failed = 0
    failures = []

    print("=" * 68)
    print(
        f"Commission Tests ({len(tests)} total)"
    )
    print("=" * 68)
    print()

    for test in tests:
        try:
            test()
            print(f"PASSED: {test.__name__}")
            passed += 1

        except AssertionError as exc:
            print(
                f"FAILED: {test.__name__}: {exc}"
            )

            failures.append(
                (test.__name__, str(exc))
            )

            failed += 1

        except Exception as exc:
            print(
                f"FAILED: {test.__name__}: "
                f"{type(exc).__name__}: {exc}"
            )

            failures.append(
                (
                    test.__name__,
                    f"{type(exc).__name__}: {exc}",
                )
            )

            failed += 1

    print()
    print("=" * 68)
    print(
        f"FINAL RESULT: {passed}/{len(tests)} PASSED"
    )
    print("=" * 68)

    if failures:
        print()
        print("Failures:")
        print()

        for name, message in failures:
            print(f"  - {name}")
            print(f"    {message}")
            print()

    if failed:
        sys.exit(1)