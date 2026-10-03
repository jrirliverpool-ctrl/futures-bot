"""
test_backtest.py — Unit tests for backtest.py.

تمام تست‌ها deterministic و بدون شبکه. از داده مصنوعی استفاده می‌شود.

پوشش:
  - Signal generation
  - Backtest behavior (uptrend / downtrend / flat)
  - Determinism (شامل event replay)
  - Equity accounting (realized + MTM)
  - Trade counting (TP1_HIT ≠ trade)
  - Metrics (PF, MaxDD, sharpe_like, calmar_like)
  - Reporting (format + JSON)
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from backtest import (
    run_backtest,
    compute_metrics,
    compute_mtm_equity,
    generate_signals,
    format_metrics,
    result_to_json,
)

from strategy_config import get_strategy_config
from compute_hash import compute_strategy_hash


# ============================================================
# Fixtures
# ============================================================

SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}


def _synthetic_uptrend(n: int, step: float = 0.5, start: float = 100.0):
    """روند صعودی یکنواخت."""
    klines = []
    price = start
    for i in range(n):
        o = price
        c = o + step
        h = c + 0.1
        l = o - 0.1
        klines.append([
            i * 3_600_000,
            str(o), str(h), str(l), str(c), "1000",
        ])
        price = c
    return klines


def _synthetic_downtrend(n: int, step: float = 0.5, start: float = 500.0):
    """روند نزولی یکنواخت."""
    klines = []
    price = start
    for i in range(n):
        o = price
        c = o - step
        h = o + 0.1
        l = c - 0.1
        klines.append([
            i * 3_600_000,
            str(o), str(h), str(l), str(c), "1000",
        ])
        price = c
    return klines


def _flat(n: int, price: float = 100.0):
    """کندل‌های flat — no signal."""
    klines = []
    for i in range(n):
        klines.append([
            i * 3_600_000,
            str(price), str(price + 0.5), str(price - 0.5),
            str(price), "1000",
        ])
    return klines


# ============================================================
# Signal Generation
# ============================================================

def test_generate_signals_count():
    klines = _synthetic_uptrend(300)
    indicators, signals = generate_signals(klines)
    assert len(signals) == 300
    assert len(indicators["closes"]) == 300


def test_generate_signals_early_warmup():
    klines = _synthetic_uptrend(50)
    _, signals = generate_signals(klines)
    assert all(s["signal"] == "NO_TRADE" for s in signals[:40])


def test_generate_signals_empty_raises():
    try:
        generate_signals([])
        assert False
    except ValueError:
        pass


# ============================================================
# Backtest Behavior
# ============================================================

def test_backtest_uptrend_long_trades():
    klines = _synthetic_uptrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    entries = [e for e in result["events"] if e["type"] == "ENTRY"]
    assert len(entries) >= 1
    assert all(e["side"] == "LONG" for e in entries)


def test_backtest_downtrend_short_trades():
    klines = _synthetic_downtrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    entries = [e for e in result["events"] if e["type"] == "ENTRY"]
    assert len(entries) >= 1
    assert all(e["side"] == "SHORT" for e in entries)


def test_backtest_flat_no_trades():
    klines = _flat(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert result["metrics"]["total_trades"] == 0
    assert abs(result["metrics"]["final_equity_realized"] - 1000.0) < 1e-9


def test_backtest_empty_raises():
    try:
        run_backtest([], SPECS)
        assert False
    except ValueError:
        pass


def test_backtest_short_data():
    klines = _synthetic_uptrend(50)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert result["metrics"]["total_trades"] == 0
    assert result["metrics"]["final_equity_realized"] == 1000.0


# ============================================================
# Determinism
# ============================================================

def test_backtest_deterministic_metrics():
    klines = _synthetic_uptrend(300)
    r1 = run_backtest(klines, SPECS, initial_equity=1000.0)
    r2 = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert r1["metrics"] == r2["metrics"]


def test_backtest_deterministic_events():
    klines = _synthetic_uptrend(300)
    r1 = run_backtest(klines, SPECS, initial_equity=1000.0)
    r2 = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert r1["events"] == r2["events"]


def test_backtest_deterministic_curves():
    klines = _synthetic_uptrend(300)
    r1 = run_backtest(klines, SPECS, initial_equity=1000.0)
    r2 = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert r1["equity_curve"] == r2["equity_curve"]
    assert r1["realized_equity_curve"] == r2["realized_equity_curve"]


def test_backtest_hash_in_metadata():
    klines = _synthetic_uptrend(300)
    result = run_backtest(klines, SPECS)
    assert result["strategy_metadata"]["hash"] == compute_strategy_hash()
    assert result["strategy_metadata"]["version"] == \
        get_strategy_config()["version"]


# ============================================================
# Equity Accounting
# ============================================================

def test_realized_equity_consistency():
    klines = _synthetic_uptrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    pnl_sum = sum(
        float(e.get("pnl_usd", 0.0) or 0.0)
        for e in result["events"]
        if e["type"] == "EXIT"
    )
    expected = 1000.0 + pnl_sum
    assert abs(result["final_state"]["equity"] - expected) < 1e-6


def test_equity_curve_length():
    klines = _synthetic_uptrend(100)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert len(result["equity_curve"]) == len(klines)
    assert len(result["realized_equity_curve"]) == len(klines)


def test_mtm_equals_realized_when_no_position():
    klines = _synthetic_uptrend(100)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    assert result["final_state"]["position"] is None
    mtm = result["equity_curve"][-1]
    realized = result["realized_equity_curve"][-1]
    assert abs(mtm - realized) < 1e-9


def test_mtm_includes_unrealized_long():
    state = {
        "equity": 1000.0,
        "position": {
            "side": "LONG",
            "entry_price": 100.0,
            "remaining_size": 1.0,
            "realized_pnl": 0.0,
        },
    }
    mtm = compute_mtm_equity(state, close_price=105.0, specs=SPECS)
    assert abs(mtm - (1000.0 + 0.005)) < 1e-12


def test_mtm_includes_unrealized_short():
    state = {
        "equity": 1000.0,
        "position": {
            "side": "SHORT",
            "entry_price": 100.0,
            "remaining_size": 1.0,
            "realized_pnl": 0.0,
        },
    }
    mtm = compute_mtm_equity(state, close_price=95.0, specs=SPECS)
    assert abs(mtm - (1000.0 + 0.005)) < 1e-12


def test_mtm_includes_tp1_realized():
    state = {
        "equity": 1000.0,
        "position": {
            "side": "LONG",
            "entry_price": 100.0,
            "remaining_size": 0.5,
            "realized_pnl": 0.004,
        },
    }
    mtm = compute_mtm_equity(state, close_price=110.0, specs=SPECS)
    assert abs(mtm - 1000.009) < 1e-12


def test_mtm_no_position():
    state = {"equity": 1500.0, "position": None}
    mtm = compute_mtm_equity(state, 100.0, SPECS)
    assert mtm == 1500.0


# ============================================================
# Trade Accounting (اصلاح‌شده)
# ============================================================

def test_one_trade_per_exit_event():
    """
    قواعد حسابداری معاملات:
      1. total_trades == number of EXIT events
      2. tp1_count ≤ entry_count (هر position حداکثر یک TP1)
      3. entry_count - exit_count ∈ {0, 1}  (پوزیشن باز در پایان backtest)
      4. tp1_count ≤ exit_count  (هر TP1 به یک EXIT منجر می‌شود، مگر پوزیشن باز)
    """
    klines = _synthetic_uptrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)

    exit_count = sum(1 for e in result["events"] if e["type"] == "EXIT")
    tp1_count = sum(1 for e in result["events"] if e["type"] == "TP1_HIT")
    entry_count = sum(1 for e in result["events"] if e["type"] == "ENTRY")

    print(
        f"  [diag] entries={entry_count}, "
        f"exits={exit_count}, "
        f"tp1={tp1_count}"
    )

    # 1. total_trades == exit_count
    assert result["metrics"]["total_trades"] == exit_count, (
        f"total_trades ({result['metrics']['total_trades']}) "
        f"!= exit_count ({exit_count})"
    )

    # 2. هر position حداکثر یک TP1_HIT → tp1_count ≤ entry_count
    assert tp1_count <= entry_count, (
        f"tp1_count ({tp1_count}) > entry_count ({entry_count}). "
        f"این یعنی TP1_HIT بدون ENTRY یا چند بار برای یک position رخ داده."
    )

    # 3. در پایان backtest، حداکثر یک position باز باقی می‌ماند
    #    (اگر پوزیشن باز باشد: entry = exit + 1)
    assert entry_count - exit_count in (0, 1), (
        f"entry_count ({entry_count}) - exit_count ({exit_count}) "
        f"باید 0 یا 1 باشد."
    )

    # 4. TP1 نمی‌تواند از EXIT بیشتر باشد (هر TP1 با یک EXIT بسته می‌شود)
    assert tp1_count <= exit_count, (
        f"tp1_count ({tp1_count}) > exit_count ({exit_count}). "
        f"TP1_HIT بدون EXIT رخ داده."
    )


def test_exit_reason_count_matches_trades():
    """مجموع exit_reason_distribution == total_trades."""
    klines = _synthetic_uptrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    dist_sum = sum(result["metrics"]["exit_reason_distribution"].values())
    assert dist_sum == result["metrics"]["total_trades"]


def test_entry_before_exit_per_position():
    """هر EXIT باید یک ENTRY قبلی داشته باشد (entry count ≥ exit count)."""
    klines = _synthetic_uptrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)

    entry_count = sum(1 for e in result["events"] if e["type"] == "ENTRY")
    exit_count = sum(1 for e in result["events"] if e["type"] == "EXIT")
    assert entry_count >= exit_count


def test_tp1_only_if_entry_exists():
    """اگر TP1_HIT وجود دارد، حتماً ENTRY وجود دارد."""
    klines = _synthetic_uptrend(400)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)

    tp1_count = sum(1 for e in result["events"] if e["type"] == "TP1_HIT")
    entry_count = sum(1 for e in result["events"] if e["type"] == "ENTRY")
    if tp1_count > 0:
        assert entry_count > 0


# ============================================================
# Metrics
# ============================================================

def test_metrics_empty():
    metrics = compute_metrics([1000.0] * 10, [], 1000.0)
    assert metrics["total_trades"] == 0
    assert metrics["net_return_pct"] == 0.0
    assert metrics["max_drawdown_pct"] == 0.0
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0


def test_metrics_max_drawdown():
    equity = [1000.0, 1100.0, 900.0, 950.0, 1050.0]
    metrics = compute_metrics(equity, [], 1000.0)
    expected = (1100.0 - 900.0) / 1100.0 * 100.0
    assert abs(metrics["max_drawdown_pct"] - expected) < 1e-6


def test_metrics_net_return():
    equity = [1000.0, 1100.0]
    metrics = compute_metrics(equity, [], 1000.0)
    assert abs(metrics["net_return_pct"] - 10.0) < 1e-9


def test_metrics_profit_factor():
    events = [
        {"type": "EXIT", "pnl_usd": 100.0, "reason": "TP1"},
        {"type": "EXIT", "pnl_usd": -50.0, "reason": "SL"},
        {"type": "EXIT", "pnl_usd": 80.0, "reason": "TP1"},
        {"type": "EXIT", "pnl_usd": -30.0, "reason": "SL"},
    ]
    metrics = compute_metrics([1000.0] * 10, events, 1000.0)
    assert metrics["total_trades"] == 4
    assert metrics["wins"] == 2
    assert metrics["losses"] == 2
    assert metrics["win_rate"] == 0.5
    assert metrics["gross_profit"] == 180.0
    assert metrics["gross_loss"] == 80.0
    assert abs(metrics["profit_factor"] - 180.0 / 80.0) < 1e-9


def test_metrics_exit_reasons():
    events = [
        {"type": "EXIT", "pnl_usd": 10.0, "reason": "TP1"},
        {"type": "EXIT", "pnl_usd": -5.0, "reason": "SL"},
        {"type": "EXIT", "pnl_usd": -5.0, "reason": "SL"},
        {"type": "EXIT", "pnl_usd": 2.0, "reason": "TIME_EXIT"},
    ]
    metrics = compute_metrics([1000.0], events, 1000.0)
    assert metrics["exit_reason_distribution"] == {
        "TP1": 1, "SL": 2, "TIME_EXIT": 1,
    }


def test_metrics_long_short_counts():
    events = [
        {"type": "ENTRY", "side": "LONG"},
        {"type": "ENTRY", "side": "LONG"},
        {"type": "ENTRY", "side": "SHORT"},
        {"type": "EXIT", "pnl_usd": 5.0, "reason": "TP1"},
    ]
    metrics = compute_metrics([1000.0], events, 1000.0)
    assert metrics["long_count"] == 2
    assert metrics["short_count"] == 1


def test_metrics_ambiguous_count():
    events = [
        {"type": "EXIT", "pnl_usd": -5.0, "reason": "SL", "ambiguous": True},
        {"type": "EXIT", "pnl_usd": -5.0, "reason": "SL", "ambiguous": False},
    ]
    metrics = compute_metrics([1000.0], events, 1000.0)
    assert metrics["ambiguous_exit_count"] == 1


def test_metrics_zero_initial_raises():
    try:
        compute_metrics([], [], 0)
        assert False
    except ValueError:
        pass


def test_metrics_infinite_profit_factor_all_wins():
    events = [
        {"type": "EXIT", "pnl_usd": 10.0, "reason": "TP1"},
        {"type": "EXIT", "pnl_usd": 5.0, "reason": "TP1"},
    ]
    metrics = compute_metrics([1000.0], events, 1000.0)
    assert metrics["profit_factor"] == float("inf")


def test_metrics_naming():
    metrics = compute_metrics([1000.0], [], 1000.0)
    assert "sharpe_like_per_trade" in metrics
    assert "calmar_like_simple" in metrics
    assert "sharpe_per_trade" not in metrics
    assert "calmar" not in metrics


# ============================================================
# Reporting
# ============================================================

def test_format_metrics_returns_string():
    klines = _synthetic_uptrend(300)
    result = run_backtest(klines, SPECS)
    out = format_metrics(result["metrics"])
    assert isinstance(out, str)
    assert "BACKTEST METRICS" in out


def test_result_to_json_serializable():
    klines = _synthetic_uptrend(300)
    result = run_backtest(klines, SPECS)
    js = result_to_json(result)
    import json as _json
    parsed = _json.loads(js)
    assert "metrics" in parsed
    assert parsed["symbol"] == "UNKNOWN"


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_generate_signals_count,
        test_generate_signals_early_warmup,
        test_generate_signals_empty_raises,
        test_backtest_uptrend_long_trades,
        test_backtest_downtrend_short_trades,
        test_backtest_flat_no_trades,
        test_backtest_empty_raises,
        test_backtest_short_data,
        test_backtest_deterministic_metrics,
        test_backtest_deterministic_events,
        test_backtest_deterministic_curves,
        test_backtest_hash_in_metadata,
        test_realized_equity_consistency,
        test_equity_curve_length,
        test_mtm_equals_realized_when_no_position,
        test_mtm_includes_unrealized_long,
        test_mtm_includes_unrealized_short,
        test_mtm_includes_tp1_realized,
        test_mtm_no_position,
        test_one_trade_per_exit_event,
        test_exit_reason_count_matches_trades,
        test_entry_before_exit_per_position,
        test_tp1_only_if_entry_exists,
        test_metrics_empty,
        test_metrics_max_drawdown,
        test_metrics_net_return,
        test_metrics_profit_factor,
        test_metrics_exit_reasons,
        test_metrics_long_short_counts,
        test_metrics_ambiguous_count,
        test_metrics_zero_initial_raises,
        test_metrics_infinite_profit_factor_all_wins,
        test_metrics_naming,
        test_format_metrics_returns_string,
        test_result_to_json_serializable,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Backtest Unit Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)