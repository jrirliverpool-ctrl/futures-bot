"""
performance.py — گزارش‌ساز Backtest برای ApexQuant V1.

مسئولیت‌ها:
  - خروجی CSV از equity curve و trades
  - Rolling drawdown از equity curve
  - Long vs Short stats جداگانه
  - Period breakdown (بر اساس candle timestamp)
  - گزارش متنی یکجا

غیرمسئولیت‌ها:
  - محاسبه metrics پایه (backtest.py)
  - اجرای backtest (backtest.py)
  - فرمت‌دهی JSON (backtest.py)

Pure Python. No pandas/numpy.
"""

import csv
import os
from datetime import datetime, timezone


# ============================================================
# Constants
# ============================================================

EQUITY_CSV_COLUMNS = (
    "candle_index",
    "candle_ts_ms",
    "candle_ts_iso",
    "equity_mtm",
    "equity_realized",
    "drawdown_pct",
    "peak_equity",
)

TRADES_CSV_COLUMNS = (
    "trade_index",
    "side",
    "entry_ts_ms",
    "exit_ts_ms",
    "entry_price",
    "exit_price",
    "size",
    "pnl_usd",
    "risk_usd",
    "r_multiple",
    "exit_reason",
    "ambiguous_exit",
    "candles_held",
)


# ============================================================
# Helpers
# ============================================================

def _ts_to_iso(ts_ms) -> str:
    if ts_ms is None or ts_ms == "":
        return ""
    try:
        return datetime.fromtimestamp(
            int(ts_ms) / 1000, tz=timezone.utc,
        ).isoformat()
    except (OSError, ValueError, OverflowError):
        return ""


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


# ============================================================
# Rolling Drawdown
# ============================================================

def compute_rolling_drawdown(equity_curve: list,
                             initial_equity: float) -> list:
    """
    DD در هر نقطه از equity curve.

    Returns:
        list of dict با فیلدهای:
          equity, peak_equity, drawdown_pct
    """
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    result = []
    peak = initial_equity
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
        else:
            dd = 0.0
        result.append({
            "equity": float(eq),
            "peak_equity": float(peak),
            "drawdown_pct": float(dd),
        })
    return result


# ============================================================
# Trade Extraction
# ============================================================

def extract_trades(events: list, contract_multiplier: float) -> list:
    """
    استخراج لیست trade های کامل از event ها.

    قرارداد event ها:
      - هر ENTRY با یک EXIT در همان ترتیب جفت می‌شود.
      - TP1_HIT به عنوان trade جداگانه شمرده نمی‌شود. این event
        فقط لاگ مدیریتی است؛ PnL نیمه‌ی TP1 در pnl_usd نهایی EXIT
        لحاظ شده است (خروجی close_position در paper_engine).
      - اگر SL در ENTRY نبود یا risk_per_unit صفر بود → R = None.

    R_multiple محاسبه می‌شود از:
        risk_per_unit = |entry_price - SL|
        risk_usd     = size × risk_per_unit × contract_multiplier
        R            = pnl_usd / risk_usd

    این روش، partial close را ذاتاً درست لحاظ می‌کند چون pnl_usd
    مجموع TP1 + باقی‌مانده است.

    Args:
        events: لیست event ها از run_backtest()["events"]
        contract_multiplier: contract multiplier نماد (اجباری).
            مثال BTC-SWAP-USDT: 0.001

    Returns:
        list of dict با فیلدهای:
          side, entry_ts_ms, exit_ts_ms, entry_price, exit_price,
          size, pnl_usd, risk_usd, r_multiple, exit_reason,
          ambiguous_exit, candles_held
    """
    if contract_multiplier is None:
        raise ValueError("contract_multiplier is required")
    if contract_multiplier <= 0:
        raise ValueError(
            f"contract_multiplier must be > 0, "
            f"got {contract_multiplier}"
        )

    trades = []
    current_entry = None

    for e in events:
        et = e.get("type")
        if et == "ENTRY":
            current_entry = {
                "side": e.get("side"),
                "entry_ts_ms": e.get("timestamp"),
                "entry_price": e.get("price"),
                "size": e.get("size"),
                "sl": e.get("sl"),
                "tp1": e.get("tp1"),
            }
        elif et == "EXIT":
            if current_entry is None:
                continue

            entry_price = float(current_entry["entry_price"])
            exit_price = float(e.get("exit_price_adj", e.get("price")))
            side = current_entry["side"]
            size = float(current_entry["size"])
            pnl_usd = float(e.get("pnl_usd", 0.0) or 0.0)

            sl = current_entry.get("sl")
            risk_per_unit = None
            risk_usd = None
            r_multiple = None

            if sl is not None:
                try:
                    risk_per_unit = abs(entry_price - float(sl))
                except (TypeError, ValueError):
                    risk_per_unit = None

            if risk_per_unit is not None and risk_per_unit > 0:
                risk_usd = size * risk_per_unit * contract_multiplier
                if risk_usd > 0:
                    r_multiple = pnl_usd / risk_usd

            trades.append({
                "side": side,
                "entry_ts_ms": current_entry["entry_ts_ms"],
                "exit_ts_ms": e.get("timestamp"),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size": size,
                "pnl_usd": pnl_usd,
                "risk_usd": risk_usd,
                "r_multiple": r_multiple,
                "exit_reason": e.get("reason", "UNKNOWN"),
                "ambiguous_exit": bool(e.get("ambiguous", False)),
                "candles_held": None,
            })
            current_entry = None

    return trades


def _enrich_candles_held(trades: list, klines: list) -> list:
    """
    محاسبه candles_held با استفاده از اختلاف index در klines.
    """
    if not klines:
        return trades

    ts_to_index = {int(k[0]): i for i, k in enumerate(klines)}
    interval_ms = None
    if len(klines) >= 2:
        interval_ms = int(klines[1][0]) - int(klines[0][0])

    for t in trades:
        e_i = ts_to_index.get(int(t["entry_ts_ms"]))
        x_i = ts_to_index.get(int(t["exit_ts_ms"]))
        if e_i is not None and x_i is not None:
            t["candles_held"] = x_i - e_i
        elif interval_ms and interval_ms > 0:
            t["candles_held"] = int(
                (int(t["exit_ts_ms"]) - int(t["entry_ts_ms"]))
                / interval_ms
            )
    return trades


# ============================================================
# Long vs Short Stats
# ============================================================

def compute_side_stats(trades: list) -> dict:
    """
    آمار جداگانه برای LONG و SHORT.

    Returns:
        {
          "LONG":  {trades, wins, losses, win_rate, avg_pnl, avg_r, pf},
          "SHORT": {...},
          "ALL":   {...},
        }
    """
    def _stats(subset):
        n = len(subset)
        if n == 0:
            return {
                "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "avg_pnl": 0.0,
                "avg_r": 0.0, "profit_factor": 0.0,
            }
        wins = [t["pnl_usd"] for t in subset if t["pnl_usd"] > 0]
        losses = [t["pnl_usd"] for t in subset if t["pnl_usd"] < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            pf = gross_profit / gross_loss
        elif gross_profit > 0:
            pf = float("inf")
        else:
            pf = 0.0

        r_values = [
            t["r_multiple"] for t in subset
            if t["r_multiple"] is not None
        ]
        avg_r = sum(r_values) / len(r_values) if r_values else 0.0

        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / n,
            "avg_pnl": sum(t["pnl_usd"] for t in subset) / n,
            "avg_r": avg_r,
            "profit_factor": pf,
        }

    long_trades = [t for t in trades if t["side"] == "LONG"]
    short_trades = [t for t in trades if t["side"] == "SHORT"]

    return {
        "LONG": _stats(long_trades),
        "SHORT": _stats(short_trades),
        "ALL": _stats(trades),
    }


# ============================================================
# Period Breakdown
# ============================================================

def compute_period_breakdown(trades: list) -> dict:
    """
    گروه‌بندی trade ها بر اساس روز، هفته، ماه (UTC).

    Returns:
        {"daily": {...}, "weekly": {...}, "monthly": {...}}
        هر value: dict[period_key] = {"trades": N, "pnl": X}
    """
    daily = {}
    weekly = {}
    monthly = {}

    for t in trades:
        ts = t.get("exit_ts_ms")
        if ts is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            continue

        day_key = dt.strftime("%Y-%m-%d")
        iso = dt.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        month_key = dt.strftime("%Y-%m")

        for key, bucket in (
            (day_key, daily),
            (week_key, weekly),
            (month_key, monthly),
        ):
            if key not in bucket:
                bucket[key] = {"trades": 0, "pnl": 0.0}
            bucket[key]["trades"] += 1
            bucket[key]["pnl"] += t["pnl_usd"]

    return {"daily": daily, "weekly": weekly, "monthly": monthly}


# ============================================================
# CSV Export
# ============================================================

def export_equity_csv(path: str, equity_curve: list,
                      realized_curve: list, klines: list,
                      initial_equity: float) -> None:
    """
    ذخیره equity curve به CSV.
    """
    if len(equity_curve) != len(klines):
        raise ValueError(
            f"equity_curve length ({len(equity_curve)}) "
            f"!= klines length ({len(klines)})"
        )

    dds = compute_rolling_drawdown(equity_curve, initial_equity)

    _ensure_parent_dir(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EQUITY_CSV_COLUMNS)
        writer.writeheader()
        for i, (eq, rl, dd, k) in enumerate(
            zip(equity_curve, realized_curve, dds, klines)
        ):
            writer.writerow({
                "candle_index": i,
                "candle_ts_ms": int(k[0]),
                "candle_ts_iso": _ts_to_iso(int(k[0])),
                "equity_mtm": float(eq),
                "equity_realized": float(rl),
                "drawdown_pct": dd["drawdown_pct"],
                "peak_equity": dd["peak_equity"],
            })


def export_trades_csv(path: str, trades: list) -> None:
    """
    ذخیره لیست trade ها به CSV.
    """
    _ensure_parent_dir(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADES_CSV_COLUMNS)
        writer.writeheader()
        for i, t in enumerate(trades):
            writer.writerow({
                "trade_index": i,
                "side": t["side"],
                "entry_ts_ms": int(t["entry_ts_ms"]),
                "exit_ts_ms": int(t["exit_ts_ms"]),
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "size": t["size"],
                "pnl_usd": t["pnl_usd"],
                "risk_usd": (
                    t["risk_usd"] if t["risk_usd"] is not None else ""
                ),
                "r_multiple": (
                    t["r_multiple"] if t["r_multiple"] is not None else ""
                ),
                "exit_reason": t["exit_reason"],
                "ambiguous_exit": t["ambiguous_exit"],
                "candles_held": (
                    t["candles_held"] if t["candles_held"] is not None else ""
                ),
            })


# ============================================================
# Full Report
# ============================================================

def generate_full_report(result: dict, trades: list) -> str:
    """
    گزارش متنی کامل از backtest + performance.
    """
    m = result["metrics"]
    side_stats = compute_side_stats(trades)
    periods = compute_period_breakdown(trades)

    lines = []
    lines.append("=" * 66)
    lines.append("📄 FULL PERFORMANCE REPORT")
    lines.append("=" * 66)
    lines.append(f"  symbol       : {result['symbol']}")
    lines.append(f"  candles      : {result['candle_count']}")
    lines.append(f"  start_index  : {result['start_index']}")
    lines.append(
        f"  strategy     : v{result['strategy_metadata']['version']}"
    )
    lines.append(f"  hash         : {result['strategy_metadata']['hash']}")
    lines.append("")

    lines.append("💰 Equity")
    lines.append(f"  initial              : {m['initial_equity']:.2f}")
    lines.append(f"  final (realized)     : {m['final_equity_realized']:.2f}")
    lines.append(f"  final (mark-to-mkt)  : {m['final_equity_mtm']:.2f}")
    lines.append(f"  net return (mtm)     : {m['net_return_pct']:+.2f}%")
    lines.append(f"  max drawdown (mtm)   : {m['max_drawdown_pct']:.2f}%")
    lines.append("")

    lines.append("📊 Long vs Short")
    for name in ("LONG", "SHORT", "ALL"):
        s = side_stats[name]
        pf = s["profit_factor"]
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        lines.append(
            f"  {name:5s}: trades={s['trades']:3d} "
            f"wins={s['wins']:3d} losses={s['losses']:3d} "
            f"win_rate={s['win_rate'] * 100:5.1f}% "
            f"avg_pnl={s['avg_pnl']:+8.4f} "
            f"avg_R={s['avg_r']:+6.3f} "
            f"PF={pf_str}"
        )
    lines.append("")

    lines.append("📅 Daily breakdown (نمونه ۵ روز آخر)")
    daily_items = sorted(periods["daily"].items())[-5:]
    if not daily_items:
        lines.append("  (بدون معامله)")
    else:
        for day, agg in daily_items:
            lines.append(
                f"  {day}: trades={agg['trades']} "
                f"pnl={agg['pnl']:+.4f}"
            )
    lines.append("")

    lines.append("📅 Weekly breakdown (نمونه ۵ هفته آخر)")
    weekly_items = sorted(periods["weekly"].items())[-5:]
    if not weekly_items:
        lines.append("  (بدون معامله)")
    else:
        for wk, agg in weekly_items:
            lines.append(
                f"  {wk}: trades={agg['trades']} "
                f"pnl={agg['pnl']:+.4f}"
            )
    lines.append("")

    lines.append("📅 Monthly breakdown")
    monthly_items = sorted(periods["monthly"].items())
    if not monthly_items:
        lines.append("  (بدون معامله)")
    else:
        for mo, agg in monthly_items:
            lines.append(
                f"  {mo}: trades={agg['trades']} "
                f"pnl={agg['pnl']:+.4f}"
            )
    lines.append("")
    lines.append("=" * 66)
    return "\n".join(lines)


# ============================================================
# High-Level API
# ============================================================

def build_performance_report(result: dict, klines: list,
                             contract_multiplier: float,
                             output_dir: str = None) -> dict:
    """
    ساخت کامل گزارش performance.

    Args:
        result: خروجی run_backtest()
        klines: همان klines ورودی
        contract_multiplier: contract multiplier نماد (اجباری).
        output_dir: اگر داده شود، CSV ها ذخیره می‌شوند

    Returns:
        dict با کلیدهای:
          trades, side_stats, periods, report_text
          و در صورت output_dir: equity_csv, trades_csv paths
    """
    trades = extract_trades(result["events"], contract_multiplier)
    trades = _enrich_candles_held(trades, klines)

    side_stats = compute_side_stats(trades)
    periods = compute_period_breakdown(trades)
    report_text = generate_full_report(result, trades)

    out = {
        "trades": trades,
        "side_stats": side_stats,
        "periods": periods,
        "report_text": report_text,
    }

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        eq_path = os.path.join(output_dir, "equity_curve.csv")
        tr_path = os.path.join(output_dir, "trades.csv")
        export_equity_csv(
            eq_path,
            result["equity_curve"],
            result["realized_equity_curve"],
            klines,
            result["initial_equity"],
        )
        export_trades_csv(tr_path, trades)
        out["equity_csv"] = eq_path
        out["trades_csv"] = tr_path

    return out