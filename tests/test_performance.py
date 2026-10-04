"""
test_performance.py — Unit tests for performance.py.

پوشش (۳۰ تست):
  - Rolling drawdown (3)
  - Trade extraction basics (5)
  - R_multiple شامل partial close + scaling invariant (6)
  - Contract multiplier validation (3)
  - Side stats (2)
  - Period breakdown (3)
  - CSV export (4)
  - Full report generation (4)
"""

import os
import sys
import tempfile

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from performance import (
    EQUITY_CSV_COLUMNS,
    TRADES_CSV_COLUMNS,
    compute_rolling_drawdown,
    extract_trades,
    compute_side_stats,
    compute_period_breakdown,
    export_equity_csv,
    export_trades_csv,
    generate_full_report,
    build_performance_report,
)

from backtest import run_backtest


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

MULT = 0.001  # BTC-SWAP-USDT contract_multiplier


def _synthetic_uptrend(n: int, step: float = 0.5, start: float = 100.0):
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


# ============================================================
# Rolling Drawdown
# ============================================================

def test_rolling_drawdown_flat():
    curve = [1000.0, 1000.0, 1000.0]
    dds = compute_rolling_drawdown(curve, 1000.0)
    assert all(d["drawdown_pct"] == 0.0 for d in dds)
    assert all(d["peak_equity"] == 1000.0 for d in dds)


def test_rolling_drawdown_simple():
    curve = [1000.0, 1100.0, 900.0, 1050.0]
    dds = compute_rolling_drawdown(curve, 1000.0)
    assert dds[0]["drawdown_pct"] == 0.0
    assert dds[1]["drawdown_pct"] == 0.0
    expected2 = (1100.0 - 900.0) / 1100.0 * 100.0
    assert abs(dds[2]["drawdown_pct"] - expected2) < 1e-9
    expected3 = (1100.0 - 1050.0) / 1100.0 * 100.0
    assert abs(dds[3]["drawdown_pct"] - expected3) < 1e-9


def test_rolling_drawdown_invalid_initial():
    try:
        compute_rolling_drawdown([1000.0], 0)
        assert False
    except ValueError:
        pass


# ============================================================
# Trade Extraction — Basics
# ============================================================

def test_extract_trades_empty():
    assert extract_trades([], MULT) == []


def test_extract_trades_single():
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "sl": 95.0, "tp1": 110.0, "timestamp": 1000},
        {"type": "EXIT", "reason": "TP1", "price": 108.0,
         "exit_price_adj": 107.9, "pnl_usd": 7.9, "timestamp": 2000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert len(trades) == 1
    assert trades[0]["side"] == "LONG"
    assert trades[0]["entry_price"] == 100.0
    assert trades[0]["exit_price"] == 107.9
    assert trades[0]["pnl_usd"] == 7.9
    assert trades[0]["exit_reason"] == "TP1"
    assert trades[0]["risk_usd"] is not None
    assert trades[0]["r_multiple"] is not None


def test_extract_trades_skips_tp1_as_trade():
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "sl": 95.0, "tp1": 110.0, "timestamp": 1000},
        {"type": "TP1_HIT", "price": 110.0, "size": 0.5,
         "timestamp": 1500},
        {"type": "EXIT", "reason": "TRAILING_SL", "price": 112.0,
         "exit_price_adj": 111.9, "pnl_usd": 11.0, "timestamp": 3000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert len(trades) == 1


def test_extract_trades_multiple():
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "sl": 95.0, "tp1": 110.0, "timestamp": 1000},
        {"type": "EXIT", "reason": "TP1", "price": 108.0,
         "exit_price_adj": 108.0, "pnl_usd": 8.0, "timestamp": 2000,
         "ambiguous": False},
        {"type": "ENTRY", "side": "SHORT", "price": 200.0, "size": 1.0,
         "sl": 210.0, "tp1": 180.0, "timestamp": 3000},
        {"type": "EXIT", "reason": "SL", "price": 210.0,
         "exit_price_adj": 210.0, "pnl_usd": -10.0, "timestamp": 4000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert len(trades) == 2
    assert trades[0]["side"] == "LONG"
    assert trades[1]["side"] == "SHORT"


def test_extract_trades_orphan_exit_skipped():
    events = [
        {"type": "EXIT", "reason": "SL", "price": 100.0,
         "pnl_usd": -5.0, "timestamp": 1000, "ambiguous": False},
    ]
    assert extract_trades(events, MULT) == []


# ============================================================
# Trade Extraction — R_multiple (Fix: partial close)
# ============================================================

def test_r_multiple_simple_long():
    """
    Entry=100, SL=95, Size=1, mult=0.001
    Exit=110, pnl=1.0 × 10 × 0.001 = 0.01
    risk_per_unit = 5, risk_usd = 0.005
    R = 0.01 / 0.005 = 2.0
    """
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "sl": 95.0, "tp1": 120.0, "timestamp": 1000},
        {"type": "EXIT", "reason": "TP1", "price": 110.0,
         "exit_price_adj": 110.0, "pnl_usd": 0.01, "timestamp": 2000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert len(trades) == 1
    assert abs(trades[0]["risk_usd"] - 0.005) < 1e-12
    assert abs(trades[0]["r_multiple"] - 2.0) < 1e-9


def test_r_multiple_simple_short():
    """
    Entry=100, SL=105, Size=1, mult=0.001
    Exit=95, pnl = 1.0 × (100-95) × 0.001 = 0.005
    risk_per_unit = 5, risk_usd = 0.005
    R = 0.005 / 0.005 = 1.0
    """
    events = [
        {"type": "ENTRY", "side": "SHORT", "price": 100.0, "size": 1.0,
         "sl": 105.0, "tp1": 80.0, "timestamp": 1000},
        {"type": "EXIT", "reason": "TP1", "price": 95.0,
         "exit_price_adj": 95.0, "pnl_usd": 0.005, "timestamp": 2000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert abs(trades[0]["r_multiple"] - 1.0) < 1e-9


def test_r_multiple_partial_close():
    """
    سناریوی کلیدی: TP1 + Final Exit
    ─────────────────────────────────
    Entry = 100.0
    SL    = 95.0  → risk_per_unit = 5.0
    Size  = 1.0
    mult  = 0.001 → risk_usd = 0.005

    TP1_HIT در 110 → 0.5 size بسته شد
      pnl_tp1 = 0.5 × (110 - 100) × 0.001 = 0.005

    Final EXIT در 105 → 0.5 size باقی‌مانده بسته شد
      pnl_final = 0.5 × (105 - 100) × 0.001 = 0.0025

    total pnl_usd = 0.005 + 0.0025 = 0.0075
    R = 0.0075 / 0.005 = 1.5   ✅

    نکته: اگر اشتباهاً از فقط exit_price=105 استفاده می‌شد:
      price_delta = 5, R_اشتباه = 5/5 = 1.0
    """
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "sl": 95.0, "tp1": 110.0, "timestamp": 1000},
        {"type": "TP1_HIT", "price": 110.0, "size": 0.5,
         "timestamp": 1500},
        {"type": "EXIT", "reason": "TRAILING_SL", "price": 105.0,
         "exit_price_adj": 105.0, "pnl_usd": 0.0075, "timestamp": 2000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert len(trades) == 1
    assert abs(trades[0]["risk_usd"] - 0.005) < 1e-12
    assert abs(trades[0]["r_multiple"] - 1.5) < 1e-9, (
        f"expected R=1.5 (partial close aware), "
        f"got {trades[0]['r_multiple']}"
    )


def test_r_multiple_no_sl_returns_none():
    """اگر SL در ENTRY نبود، R باید None باشد."""
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "tp1": 110.0, "timestamp": 1000},  # بدون sl
        {"type": "EXIT", "reason": "TP1", "price": 110.0,
         "exit_price_adj": 110.0, "pnl_usd": 0.01, "timestamp": 2000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert len(trades) == 1
    assert trades[0]["risk_usd"] is None
    assert trades[0]["r_multiple"] is None


def test_r_multiple_zero_risk_returns_none():
    """اگر Entry == SL، risk_per_unit = 0 → R = None."""
    events = [
        {"type": "ENTRY", "side": "LONG", "price": 100.0, "size": 1.0,
         "sl": 100.0, "tp1": 110.0, "timestamp": 1000},
        {"type": "EXIT", "reason": "TP1", "price": 110.0,
         "exit_price_adj": 110.0, "pnl_usd": 0.01, "timestamp": 2000,
         "ambiguous": False},
    ]
    trades = extract_trades(events, MULT)
    assert trades[0]["risk_usd"] is None
    assert trades[0]["r_multiple"] is None


def test_r_multiple_scales_with_multiplier():
    """
    contract_multiplier scaling invariant:
    اگر multiplier دو برابر شود:
      risk_usd → 2x
      R = pnl / risk_usd → 0.5x

    این تست تضمین می‌کند R به‌صورت صحیح نسبت به
    contract multiplier محاسبه می‌شود، بدون اینکه به عدد
    خاصی وابسته باشد.
    """
    events = [
        {
            "type": "ENTRY",
            "side": "LONG",
            "price": 100.0,
            "size": 1.0,
            "sl": 95.0,
            "tp1": 110.0,
            "timestamp": 1000,
        },
        {
            "type": "EXIT",
            "reason": "TP1",
            "price": 110.0,
            "exit_price_adj": 110.0,
            "pnl_usd": 0.01,
            "timestamp": 2000,
            "ambiguous": False,
        },
    ]

    t1 = extract_trades(events, 0.001)[0]
    t2 = extract_trades(events, 0.002)[0]

    assert abs(t2["risk_usd"] - 2.0 * t1["risk_usd"]) < 1e-12
    assert abs(t2["r_multiple"] - t1["r_multiple"] / 2.0) < 1e-12


# ============================================================
# Contract Multiplier Validation
# ============================================================

def test_extract_trades_multiplier_none_raises():
    try:
        extract_trades([], None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_extract_trades_multiplier_zero_raises():
    try:
        extract_trades([], 0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_extract_trades_multiplier_negative_raises():
    try:
        extract_trades([], -0.001)
        assert False, "expected ValueError"
    except ValueError:
        pass


# ============================================================
# Side Stats
# ============================================================

def test_side_stats_empty():
    stats = compute_side_stats([])
    assert stats["LONG"]["trades"] == 0
    assert stats["SHORT"]["trades"] == 0
    assert stats["ALL"]["trades"] == 0
    assert stats["LONG"]["win_rate"] == 0.0


def test_side_stats_mixed():
    trades = [
        {"side": "LONG", "pnl_usd": 10.0, "r_multiple": 2.0},
        {"side": "LONG", "pnl_usd": -5.0, "r_multiple": -1.0},
        {"side": "SHORT", "pnl_usd": 8.0, "r_multiple": 1.6},
        {"side": "SHORT", "pnl_usd": -4.0, "r_multiple": -0.8},
    ]
    stats = compute_side_stats(trades)
    assert stats["LONG"]["trades"] == 2
    assert stats["LONG"]["wins"] == 1
    assert stats["LONG"]["losses"] == 1
    assert stats["LONG"]["win_rate"] == 0.5
    assert stats["LONG"]["profit_factor"] == 10.0 / 5.0
    assert stats["SHORT"]["trades"] == 2
    assert stats["ALL"]["trades"] == 4
    assert abs(stats["LONG"]["avg_r"] - 0.5) < 1e-9


# ============================================================
# Period Breakdown
# ============================================================

def test_period_breakdown_empty():
    periods = compute_period_breakdown([])
    assert periods["daily"] == {}
    assert periods["weekly"] == {}
    assert periods["monthly"] == {}


def test_period_breakdown_single_day():
    # 2026-10-04 00:00 UTC
    ts = 1791168000000
    trades = [
        {"exit_ts_ms": ts, "pnl_usd": 5.0},
        {"exit_ts_ms": ts + 1000, "pnl_usd": -2.0},
    ]
    periods = compute_period_breakdown(trades)
    assert len(periods["daily"]) == 1
    day_key = list(periods["daily"].keys())[0]
    assert periods["daily"][day_key]["trades"] == 2
    assert abs(periods["daily"][day_key]["pnl"] - 3.0) < 1e-9


def test_period_breakdown_multi_month():
    # 2026-01-15 00:00 UTC و 2026-02-19 00:00 UTC
    jan = 1768435200000
    feb = 1771545600000
    trades = [
        {"exit_ts_ms": jan, "pnl_usd": 3.0},
        {"exit_ts_ms": feb, "pnl_usd": 4.0},
    ]
    periods = compute_period_breakdown(trades)
    assert len(periods["monthly"]) == 2


# ============================================================
# CSV Export
# ============================================================

def test_export_equity_csv():
    klines = _synthetic_uptrend(10)
    equity_curve = [1000.0 + i for i in range(10)]
    realized = [1000.0] * 10
    path = _tmp_path(".csv")
    try:
        export_equity_csv(path, equity_curve, realized, klines, 1000.0)
        assert os.path.exists(path)
        with open(path, "r") as f:
            header = f.readline().strip()
        assert header == ",".join(EQUITY_CSV_COLUMNS)
        with open(path, "r") as f:
            lines = f.readlines()
        assert len(lines) == 1 + 10
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_export_equity_length_mismatch():
    klines = _synthetic_uptrend(5)
    try:
        export_equity_csv(
            _tmp_path(".csv"), [1000.0] * 3, [1000.0] * 3, klines, 1000.0,
        )
        assert False
    except ValueError:
        pass


def test_export_trades_csv():
    trades = [
        {"side": "LONG", "entry_ts_ms": 1000, "exit_ts_ms": 2000,
         "entry_price": 100.0, "exit_price": 110.0, "size": 1.0,
         "pnl_usd": 10.0, "risk_usd": 5.0, "r_multiple": 2.0,
         "exit_reason": "TP1", "ambiguous_exit": False,
         "candles_held": 5},
    ]
    path = _tmp_path(".csv")
    try:
        export_trades_csv(path, trades)
        with open(path, "r") as f:
            header = f.readline().strip()
        assert header == ",".join(TRADES_CSV_COLUMNS)
        with open(path, "r") as f:
            lines = f.readlines()
        assert len(lines) == 2
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_export_trades_handles_none_r():
    trades = [
        {"side": "LONG", "entry_ts_ms": 1000, "exit_ts_ms": 2000,
         "entry_price": 100.0, "exit_price": 110.0, "size": 1.0,
         "pnl_usd": 10.0, "risk_usd": None, "r_multiple": None,
         "exit_reason": "TP1", "ambiguous_exit": False,
         "candles_held": None},
    ]
    path = _tmp_path(".csv")
    try:
        export_trades_csv(path, trades)
        with open(path, "r") as f:
            content = f.read()
        assert "TP1" in content
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Full Report
# ============================================================

def test_full_report_contains_sections():
    klines = _synthetic_uptrend(300)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    trades = extract_trades(result["events"], MULT)
    report = generate_full_report(result, trades)
    assert "FULL PERFORMANCE REPORT" in report
    assert "Equity" in report
    assert "Long vs Short" in report
    assert "Daily breakdown" in report
    assert "Weekly breakdown" in report
    assert "Monthly breakdown" in report


def test_build_performance_report_no_export():
    klines = _synthetic_uptrend(300)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    perf = build_performance_report(result, klines, MULT)
    assert "trades" in perf
    assert "side_stats" in perf
    assert "periods" in perf
    assert "report_text" in perf
    assert "equity_csv" not in perf


def test_build_performance_report_with_export():
    klines = _synthetic_uptrend(300)
    result = run_backtest(klines, SPECS, initial_equity=1000.0)
    tmp_dir = tempfile.mkdtemp()
    try:
        perf = build_performance_report(
            result, klines, MULT, output_dir=tmp_dir,
        )
        assert os.path.exists(perf["equity_csv"])
        assert os.path.exists(perf["trades_csv"])
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_build_report_deterministic():
    klines = _synthetic_uptrend(300)
    r = run_backtest(klines, SPECS, initial_equity=1000.0)
    p1 = build_performance_report(r, klines, MULT)
    p2 = build_performance_report(r, klines, MULT)
    assert p1["report_text"] == p2["report_text"]
    assert p1["trades"] == p2["trades"]


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Rolling DD (3)
        test_rolling_drawdown_flat,
        test_rolling_drawdown_simple,
        test_rolling_drawdown_invalid_initial,
        # Extraction basics (5)
        test_extract_trades_empty,
        test_extract_trades_single,
        test_extract_trades_skips_tp1_as_trade,
        test_extract_trades_multiple,
        test_extract_trades_orphan_exit_skipped,
        # R_multiple (6)
        test_r_multiple_simple_long,
        test_r_multiple_simple_short,
        test_r_multiple_partial_close,
        test_r_multiple_no_sl_returns_none,
        test_r_multiple_zero_risk_returns_none,
        test_r_multiple_scales_with_multiplier,
        # Multiplier validation (3)
        test_extract_trades_multiplier_none_raises,
        test_extract_trades_multiplier_zero_raises,
        test_extract_trades_multiplier_negative_raises,
        # Side stats (2)
        test_side_stats_empty,
        test_side_stats_mixed,
        # Period breakdown (3)
        test_period_breakdown_empty,
        test_period_breakdown_single_day,
        test_period_breakdown_multi_month,
        # CSV export (4)
        test_export_equity_csv,
        test_export_equity_length_mismatch,
        test_export_trades_csv,
        test_export_trades_handles_none_r,
        # Full report (4)
        test_full_report_contains_sections,
        test_build_performance_report_no_export,
        test_build_performance_report_with_export,
        test_build_report_deterministic,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Performance Unit Tests")
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