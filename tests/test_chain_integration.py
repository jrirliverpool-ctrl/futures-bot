"""
test_chain_integration.py — Chain Integration Test برای ApexQuant V1.

هدف: تثبیت زنجیره‌ی کامل از داده تا گزارش.

  data → indicators → strategy → paper_engine → backtest
       → performance → state_manager → CSV → report

نکته‌ی warmup:
  استراتژی ApexQuant V1 به ۲۲۰ کندل warmup نیاز دارد.
  فازهای directional عمداً بعد از warmup قرار گرفته‌اند.

نکته‌ی factory:
  _realistic_klines(n) یک factory عمومی است. اگر n از هر
  phase کوچک‌تر باشد، فقط تا همان n تولید می‌شود و هیچ خطایی
  داده نمی‌شود. تست‌های short data از این رفتار استفاده می‌کنند.

تمام تست‌ها deterministic و بدون شبکه.
"""

import os
import sys
import tempfile
import shutil

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from backtest import run_backtest
from performance import (
    build_performance_report,
    extract_trades,
    EQUITY_CSV_COLUMNS,
    TRADES_CSV_COLUMNS,
)
from state_manager import save_state, load_state, delete_state
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

MULT = 0.001
INITIAL_EQUITY = 1000.0

# warmup استراتژی ApexQuant V1 = 220 کندل
WARMUP = 220


def _realistic_klines(n: int = 400):
    """
    داده مصنوعی realistic و deterministic (factory عمومی).

    ساختار فازها:
      phase 0 : 0..219     → warmup (خنثی)
      phase 1 : 220..299   → uptrend
      phase 2 : 300..359   → downtrend
      phase 3 : 360+       → chop

    اگر n از هر phase کوچک‌تر باشد، فقط تا n تولید می‌شود.
    مثال: n=50 → 50 کندل warmup؛ n=400 → همه‌ی فازها.

    LCG-based noise → کاملاً deterministic.
    """
    klines = []
    price = 100.0
    for i in range(n):
        if i < WARMUP:
            drift = 0.0
            vol = 0.02
        elif i < 300:
            drift = 0.15
            vol = 0.05
        elif i < 360:
            drift = -0.15
            vol = 0.05
        else:
            drift = 0.0
            vol = 0.15

        noise = ((i * 9301 + 49297) % 233280) / 233280.0 - 0.5
        noise *= vol

        o = price
        c = o + drift + noise
        h = max(o, c) + 0.02
        l = min(o, c) - 0.02
        klines.append([
            i * 3_600_000,
            str(o), str(h), str(l), str(c), "1000",
        ])
        price = c
    return klines


def _flat(n: int, price: float = 100.0):
    klines = []
    for i in range(n):
        klines.append([
            i * 3_600_000,
            str(price), str(price + 0.5), str(price - 0.5),
            str(price), "1000",
        ])
    return klines


def _tmp_path(suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.remove(path)
    return path


def _tmp_dir():
    return tempfile.mkdtemp()


def _dump_directional_diagnostics(result: dict, label: str):
    """چاپ وضعیت ENTRY های بعد از warmup برای debugging."""
    events = result["events"]
    entries = [e for e in events if e["type"] == "ENTRY"]
    entries_after = [
        e for e in entries
        if e.get("candle_index", 0) >= WARMUP
    ]
    signals = [e for e in events if e["type"] == "SIGNAL"]
    signals_after = [
        e for e in signals
        if e.get("candle_index", 0) >= WARMUP
    ]

    print(f"  [diag-{label}] total events      : {len(events)}")
    print(f"  [diag-{label}] total entries     : {len(entries)}")
    print(f"  [diag-{label}] entries >= WARMUP : {len(entries_after)}")
    print(f"  [diag-{label}] signals >= WARMUP : {len(signals_after)}")

    for e in entries_after[:10]:
        print(
            f"  [diag-{label}]   entry idx={e['candle_index']} "
            f"side={e['side']} ts={e['timestamp']}"
        )


# ============================================================
# 1. End-to-End Pipeline
# ============================================================

def test_chain_runs_end_to_end():
    klines = _realistic_klines(400)
    result = run_backtest(
        klines, SPECS, initial_equity=INITIAL_EQUITY, symbol="TEST",
    )
    tmp = _tmp_dir()
    try:
        perf = build_performance_report(
            result, klines, MULT, output_dir=tmp,
        )
        assert os.path.exists(perf["equity_csv"])
        assert os.path.exists(perf["trades_csv"])
        assert "report_text" in perf
        assert len(perf["report_text"]) > 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ test_chain_runs_end_to_end")


def test_chain_state_final_valid():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    state = result["final_state"]
    assert state["version"] == "1.0.0"
    assert state["strategy_hash"] == compute_strategy_hash()
    assert state["strategy_version"] == get_strategy_config()["version"]
    assert "equity" in state
    assert "peak_equity" in state
    assert state["peak_equity"] >= state["equity"]
    print("✅ test_chain_state_final_valid")


# ============================================================
# 2. Event Invariants
# ============================================================

def test_chain_events_balance():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    metrics = result["metrics"]
    assert metrics["total_events"] == sum(
        metrics["event_counts"].values()
    )
    print("✅ test_chain_events_balance")


def test_chain_events_ordered_by_timestamp():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    events = result["events"]
    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps)
    print("✅ test_chain_events_ordered_by_timestamp")


def test_chain_events_seq_monotonic():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    seqs = [e["seq"] for e in result["events"]]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    print("✅ test_chain_events_seq_monotonic")


# ============================================================
# 3. Equity Accounting (cross-layer)
# ============================================================

def test_chain_equity_curve_length():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    assert len(result["equity_curve"]) == len(klines)
    assert len(result["realized_equity_curve"]) == len(klines)
    print("✅ test_chain_equity_curve_length")


def test_chain_equity_consistency():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    pnl_sum = sum(
        float(e.get("pnl_usd", 0.0) or 0.0)
        for e in result["events"]
        if e["type"] == "EXIT"
    )
    expected = INITIAL_EQUITY + pnl_sum
    assert abs(result["final_state"]["equity"] - expected) < 1e-9
    print("✅ test_chain_equity_consistency")


def test_chain_trades_sum_matches_realized():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    trades = extract_trades(result["events"], MULT)

    realized_delta = (
        result["final_state"]["equity"] - INITIAL_EQUITY
    )
    trades_sum = sum(t["pnl_usd"] for t in trades)

    assert abs(trades_sum - realized_delta) < 1e-9
    print("✅ test_chain_trades_sum_matches_realized")


# ============================================================
# 4. CSV Round-trip
# ============================================================

def test_chain_csv_equity_roundtrip():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    tmp = _tmp_dir()
    try:
        perf = build_performance_report(result, klines, MULT, tmp)
        with open(perf["equity_csv"], "r") as f:
            lines = f.readlines()
        assert len(lines) == 1 + len(klines)
        header = lines[0].strip().split(",")
        assert header == list(EQUITY_CSV_COLUMNS)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ test_chain_csv_equity_roundtrip")


def test_chain_csv_trades_roundtrip():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    tmp = _tmp_dir()
    try:
        perf = build_performance_report(result, klines, MULT, tmp)
        with open(perf["trades_csv"], "r") as f:
            lines = f.readlines()
        header = lines[0].strip().split(",")
        assert header == list(TRADES_CSV_COLUMNS)
        assert len(lines) == 1 + len(perf["trades"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ test_chain_csv_trades_roundtrip")


# ============================================================
# 5. Report Content
# ============================================================

def test_chain_report_sections():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    perf = build_performance_report(result, klines, MULT)
    r = perf["report_text"]

    required_sections = [
        "FULL PERFORMANCE REPORT",
        "Equity",
        "Long vs Short",
        "Daily breakdown",
        "Weekly breakdown",
        "Monthly breakdown",
    ]
    for s in required_sections:
        assert s in r, f"missing section: {s}"
    print("✅ test_chain_report_sections")


def test_chain_metrics_all_keys():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    m = result["metrics"]

    required = [
        "initial_equity",
        "final_equity_mtm",
        "final_equity_realized",
        "net_return_pct",
        "max_drawdown_pct",
        "total_trades",
        "wins",
        "losses",
        "win_rate",
        "gross_profit",
        "gross_loss",
        "profit_factor",
        "expectancy",
        "sharpe_like_per_trade",
        "calmar_like_simple",
        "long_count",
        "short_count",
        "ambiguous_exit_count",
        "exit_reason_distribution",
        "event_counts",
        "total_events",
    ]
    for k in required:
        assert k in m, f"missing metric: {k}"
    print("✅ test_chain_metrics_all_keys")


def test_chain_side_stats_present():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    perf = build_performance_report(result, klines, MULT)
    ss = perf["side_stats"]
    for k in ("LONG", "SHORT", "ALL"):
        assert k in ss
        for field in ("trades", "wins", "losses", "win_rate",
                      "avg_pnl", "avg_r", "profit_factor"):
            assert field in ss[k]
    print("✅ test_chain_side_stats_present")


# ============================================================
# 6. State Manager Integration
# ============================================================

def test_chain_state_save_load_roundtrip():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    path = _tmp_path(".json")
    try:
        save_state(path, result["final_state"])
        loaded = load_state(path, strict=True)
        assert loaded["equity"] == result["final_state"]["equity"]
        assert loaded["strategy_hash"] == compute_strategy_hash()
    finally:
        if os.path.exists(path):
            delete_state(path)
    print("✅ test_chain_state_save_load_roundtrip")


# ============================================================
# 7. Determinism
# ============================================================

def test_chain_full_determinism():
    klines = _realistic_klines(400)
    r1 = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    r2 = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)

    assert r1["events"] == r2["events"]
    assert r1["equity_curve"] == r2["equity_curve"]
    assert r1["realized_equity_curve"] == r2["realized_equity_curve"]
    assert r1["metrics"] == r2["metrics"]
    assert r1["final_state"] == r2["final_state"]
    print("✅ test_chain_full_determinism")


def test_chain_report_deterministic():
    klines = _realistic_klines(400)
    r1 = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    r2 = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    p1 = build_performance_report(r1, klines, MULT)
    p2 = build_performance_report(r2, klines, MULT)
    assert p1["report_text"] == p2["report_text"]
    assert p1["trades"] == p2["trades"]
    assert p1["side_stats"] == p2["side_stats"]
    print("✅ test_chain_report_deterministic")


# ============================================================
# 8. Negative Cases
# ============================================================

def test_chain_empty_data_raises():
    try:
        run_backtest([], SPECS)
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_chain_empty_data_raises")


def test_chain_short_data_no_trades():
    """
    داده کوتاه (50 کندل، کاملاً داخل warmup) → صفر trade.

    چون warmup = 220 و ما 50 کندل داریم، هیچ سیگنالی
    تولید نمی‌شود و equity بدون تغییر می‌ماند.
    """
    klines = _realistic_klines(50)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    assert result["metrics"]["total_trades"] == 0
    assert result["metrics"]["final_equity_realized"] == INITIAL_EQUITY
    print("✅ test_chain_short_data_no_trades")


def test_chain_flat_data_no_trades():
    klines = _flat(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    assert result["metrics"]["total_trades"] == 0
    assert abs(
        result["metrics"]["final_equity_realized"] - INITIAL_EQUITY
    ) < 1e-9
    print("✅ test_chain_flat_data_no_trades")


# ============================================================
# 9. Directional Sanity (با diagnostic)
# ============================================================

def test_chain_uptrend_phase_produces_long():
    """
    فاز uptrend (220..299) حداقل یک LONG تولید می‌کند.

    در صورت شکست، diagnostic چاپ می‌شود تا مشخص شود
    مشکل از fixture است یا رفتار واقعی استراتژی.
    """
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)

    long_entries = [
        e for e in result["events"]
        if e["type"] == "ENTRY" and e["side"] == "LONG"
    ]

    if len(long_entries) < 1:
        _dump_directional_diagnostics(result, "UP")

    assert len(long_entries) >= 1, (
        f"expected >=1 LONG entry in uptrend phase "
        f"(220..299), got {len(long_entries)}"
    )
    print(
        f"✅ test_chain_uptrend_phase_produces_long "
        f"({len(long_entries)} entries)"
    )


def test_chain_downtrend_phase_produces_short():
    """
    فاز downtrend (300..359) حداقل یک SHORT تولید می‌کند.
    """
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)

    short_entries = [
        e for e in result["events"]
        if e["type"] == "ENTRY" and e["side"] == "SHORT"
    ]

    if len(short_entries) < 1:
        _dump_directional_diagnostics(result, "DOWN")

    assert len(short_entries) >= 1, (
        f"expected >=1 SHORT entry in downtrend phase "
        f"(300..359), got {len(short_entries)}"
    )
    print(
        f"✅ test_chain_downtrend_phase_produces_short "
        f"({len(short_entries)} entries)"
    )


# ============================================================
# 10. Sanity
# ============================================================

def test_chain_trade_risk_is_positive():
    klines = _realistic_klines(400)
    result = run_backtest(klines, SPECS, initial_equity=INITIAL_EQUITY)
    trades = extract_trades(result["events"], MULT)

    for t in trades:
        if t["risk_usd"] is None:
            continue
        assert t["risk_usd"] > 0
    print("✅ test_chain_trade_risk_is_positive")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # 1. End-to-End
        test_chain_runs_end_to_end,
        test_chain_state_final_valid,
        # 2. Events
        test_chain_events_balance,
        test_chain_events_ordered_by_timestamp,
        test_chain_events_seq_monotonic,
        # 3. Equity
        test_chain_equity_curve_length,
        test_chain_equity_consistency,
        test_chain_trades_sum_matches_realized,
        # 4. CSV
        test_chain_csv_equity_roundtrip,
        test_chain_csv_trades_roundtrip,
        # 5. Report
        test_chain_report_sections,
        test_chain_metrics_all_keys,
        test_chain_side_stats_present,
        # 6. State
        test_chain_state_save_load_roundtrip,
        # 7. Determinism
        test_chain_full_determinism,
        test_chain_report_deterministic,
        # 8. Negative
        test_chain_empty_data_raises,
        test_chain_short_data_no_trades,
        test_chain_flat_data_no_trades,
        # 9. Directional (با diagnostic)
        test_chain_uptrend_phase_produces_long,
        test_chain_downtrend_phase_produces_short,
        # 10. Sanity
        test_chain_trade_risk_is_positive,
    ]

    passed, failed = 0, 0
    print("=" * 66)
    print("🧪 Chain Integration Tests")
    print("=" * 66)
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 66)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 66)
    if failed:
        sys.exit(1)