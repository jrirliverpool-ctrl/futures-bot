"""
backtest.py — Historical backtest engine برای ApexQuant V1.

مسئولیت‌ها:
  - اجرای Paper Engine روی داده تاریخی
  - تولید signals از استراتژی برای هر کندل
  - ثبت equity curve (هم realized و هم mark-to-market)
  - محاسبه metrics عملکرد

غیرمسئولیت‌ها:
  - تصمیم‌گیری استراتژی (strategy.py)
  - مدیریت پوزیشن (paper_engine.py)
  - ذخیره state (state_manager.py)

قراردادها
══════════════════════════════════════════════════════════════

1. Realized Equity:
     state["equity"] = initial + Σ(pnl_usd of closed trades)
     فقط با close_position تغییر می‌کند.
     برای position sizing و risk استفاده می‌شود.

2. Mark-to-Market (MTM) Equity:
     mtm(i) = state["equity"]
            + pos.realized_pnl   (از TP1 که هنوز به equity اضافه نشده)
            + unrealized_pnl     (remaining_size × (close - entry) × mult × dir)
     برای equity curve و max drawdown استفاده می‌شود.
     اگر پوزیشن بازی وجود نداشته باشد: mtm == realized.

3. Exit Events vs Trades:
     هر EXIT event = بستن یک پوزیشن = یک trade.
     TP1_HIT یک event جداست و در شمارش trade لحاظ نمی‌شود.
     pnl_usd در EXIT، مجموع TP1 PnL + PnL باقی‌مانده است.

4. Fee/Commission:
     فعلاً فقط Slippage لحاظ می‌شود.
     Commission مستقل در نسخه‌های بعدی اضافه خواهد شد.

5. start_index:
     برای OOS/Walk-forward بعدی. indicators روی کل داده محاسبه می‌شوند
     (backward-only، بدون look-ahead) ولی paper_engine فقط از start_index اجرا می‌شود.

Spec: docs/strategy_v1.md
"""

import json

from indicators import compute_all
from strategy import evaluate_signal
from paper_engine import process_candle
from strategy_config import (
    get_indicator_params,
    get_engine_config,
)
from compute_hash import get_strategy_metadata
from state_manager import default_state


# ============================================================
# Equity Helpers
# ============================================================

def compute_mtm_equity(state: dict, close_price: float, specs: dict) -> float:
    """
    Mark-to-market equity در پایان یک کندل.

    mtm = state["equity"]              (realized closed trades)
        + pos.realized_pnl             (TP1 partial، در انتظار close_position)
        + unrealized_pnl               (remaining_size vs close)

    Args:
        state: state بعد از process_candle (position ممکن است None باشد)
        close_price: قیمت بسته شدن کندل جاری
        specs: symbol specs (contract_multiplier لازم است)

    Returns:
        float: MTM equity
    """
    realized = state["equity"]
    pos = state.get("position")
    if pos is None:
        return realized

    mult = specs["contract_multiplier"]
    entry = pos["entry_price"]
    remaining = pos["remaining_size"]
    side = pos["side"]
    dir_sign = 1 if side == "LONG" else -1

    tp1_realized = pos.get("realized_pnl", 0.0)
    unrealized = remaining * (close_price - entry) * mult * dir_sign

    return realized + tp1_realized + unrealized


# ============================================================
# Signal Generation
# ============================================================

def generate_signals(klines: list) -> tuple:
    """
    تولید indicators + signals برای همه‌ی کندل‌ها.

    Returns:
        (indicators, signals) — هر دو به طول len(klines)

    Note:
        indicators روی کل داده محاسبه می‌شوند (backward-only).
        signals[i] از evaluate_signal(indicators, i) می‌آید.
        indices 0 و warmup → NO_TRADE.
    """
    if not klines:
        raise ValueError("klines is empty")

    ind_params = get_indicator_params()
    indicators = compute_all(
        klines,
        ema_fast_p=ind_params["ema_fast_p"],
        ema_slow_p=ind_params["ema_slow_p"],
        donchian_p=ind_params["donchian_p"],
        atr_p=ind_params["atr_p"],
    )

    signals = []
    for i in range(len(klines)):
        sig = evaluate_signal(indicators, i)
        signals.append(sig)

    return indicators, signals


# ============================================================
# Core Backtest
# ============================================================

def run_backtest(
    klines: list,
    specs: dict,
    initial_equity: float = 1000.0,
    symbol: str = "UNKNOWN",
    start_index: int = 0,
) -> dict:
    """
    اجرای کامل backtest روی داده تاریخی.

    Args:
        klines: لیست کندل‌های خام [ts_ms, o, h, l, c, v, ...]
        specs: symbol specs (contract_multiplier, min_qty, ...)
        initial_equity: سرمایه اولیه
        symbol: نام نماد (فقط برای لاگ)
        start_index: از کدام کندل شروع شود

    Returns:
        dict با کلیدهای:
          - symbol, candle_count, start_index
          - initial_equity
          - final_state (state نهایی paper_engine)
          - events (لیست کامل event ها)
          - equity_curve (mark-to-market در پایان هر کندل)
          - realized_equity_curve (state["equity"] در پایان هر کندل)
          - metrics (dict)
          - strategy_metadata (version, hash)
    """
    if not klines:
        raise ValueError("klines is empty")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    # 1. Indicators + Signals
    indicators, signals = generate_signals(klines)

    # 2. Initial state (با strategy metadata)
    state = default_state(initial_equity)

    # 3. Engine config از strategy_config + injection
    engine_config = get_engine_config()
    engine_config["contract_multiplier_for_pnl"] = specs["contract_multiplier"]

    # 4. Loop
    mtm_curve = []
    realized_curve = []
    all_events = []
    seq = 0

    for i in range(start_index, len(klines)):
        state, events = process_candle(
            state, klines, indicators, i,
            specs, engine_config,
            signal=signals[i],
        )
        for e in events:
            e["seq"] = seq
            e["candle_index"] = i
            seq += 1
        all_events.extend(events)

        # محاسبه MTM با close کندل جاری
        close_price = float(klines[i][4])
        mtm = compute_mtm_equity(state, close_price, specs)
        mtm_curve.append(mtm)
        realized_curve.append(state["equity"])

    # 5. Metrics (از MTM curve)
    metrics = compute_metrics(
        mtm_curve, all_events, initial_equity,
        realized_final=state["equity"],
    )

    return {
        "symbol": symbol,
        "candle_count": len(klines),
        "start_index": start_index,
        "initial_equity": float(initial_equity),
        "final_state": state,
        "events": all_events,
        "equity_curve": mtm_curve,
        "realized_equity_curve": realized_curve,
        "metrics": metrics,
        "strategy_metadata": get_strategy_metadata(),
    }


# ============================================================
# Metrics
# ============================================================

def compute_metrics(
    equity_curve: list,
    events: list,
    initial_equity: float,
    realized_final: float = None,
) -> dict:
    """
    محاسبه metrics عملکرد از MTM equity curve و events.

    تمام metrics deterministic و بدون وابستگی خارجی.

    Args:
        equity_curve: MTM curve
        events: لیست event ها
        initial_equity: equity اولیه
        realized_final: state["equity"] نهایی (برای cross-check)

    Returns:
        dict metrics
    """
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    mtm_final = equity_curve[-1] if equity_curve else initial_equity
    if realized_final is None:
        realized_final = mtm_final

    net_return_pct = (mtm_final - initial_equity) / initial_equity * 100.0

    # --- Max drawdown از MTM curve ---
    peak = initial_equity
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    max_dd_pct = max_dd * 100.0

    # --- Trade stats (فقط EXIT events) ---
    # نکته: TP1_HIT نوع جداگانه‌ای دارد و این‌جا شمرده نمی‌شود.
    # هر EXIT = یک trade بسته‌شده.
    exit_events = [e for e in events if e.get("type") == "EXIT"]
    trade_pnls = [
        float(e.get("pnl_usd", 0.0) or 0.0) for e in exit_events
    ]
    total_trades = len(trade_pnls)

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]

    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = sum(trade_pnls) / total_trades if total_trades > 0 else 0.0

    # --- Exit reason distribution ---
    exit_reasons = {}
    for e in exit_events:
        r = e.get("reason", "UNKNOWN")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # --- Long / Short counts ---
    entry_events = [e for e in events if e.get("type") == "ENTRY"]
    long_count = sum(1 for e in entry_events if e.get("side") == "LONG")
    short_count = sum(1 for e in entry_events if e.get("side") == "SHORT")

    # --- Ambiguous exits ---
    ambiguous_count = sum(1 for e in exit_events if e.get("ambiguous"))

    # --- Per-trade Sharpe-like metric (نه Sharpe استاندارد) ---
    if len(trade_pnls) >= 2:
        mean_pnl = sum(trade_pnls) / len(trade_pnls)
        variance = sum(
            (p - mean_pnl) ** 2 for p in trade_pnls
        ) / (len(trade_pnls) - 1)
        std = variance ** 0.5
        sharpe_like_per_trade = mean_pnl / std if std > 0 else 0.0
    else:
        sharpe_like_per_trade = 0.0

    # --- Simple Calmar-like metric (نه Calmar سالانه استاندارد) ---
    if max_dd_pct > 0:
        calmar_like_simple = net_return_pct / max_dd_pct
    else:
        calmar_like_simple = (
            float("inf") if net_return_pct > 0 else 0.0
        )

    # --- Event counts ---
    event_counts = {}
    for e in events:
        t = e.get("type", "UNKNOWN")
        event_counts[t] = event_counts.get(t, 0) + 1

    return {
        # Equity
        "initial_equity": float(initial_equity),
        "final_equity_mtm": float(mtm_final),
        "final_equity_realized": float(realized_final),
        "net_return_pct": float(net_return_pct),
        "max_drawdown_pct": float(max_dd_pct),
        # Trade stats
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": float(win_rate),
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "profit_factor": float(profit_factor),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "expectancy": float(expectancy),
        # Risk-adjusted (نام‌های دقیق‌تر)
        "sharpe_like_per_trade": float(sharpe_like_per_trade),
        "calmar_like_simple": float(calmar_like_simple),
        # Composition
        "long_count": long_count,
        "short_count": short_count,
        "ambiguous_exit_count": ambiguous_count,
        "exit_reason_distribution": exit_reasons,
        "event_counts": event_counts,
        "total_events": len(events),
    }


# ============================================================
# Reporting
# ============================================================

def format_metrics(metrics: dict) -> str:
    """فرمت‌بندی خوانا metrics برای چاپ."""
    lines = []
    lines.append("=" * 60)
    lines.append("📊 BACKTEST METRICS")
    lines.append("=" * 60)

    lines.append("💰 Equity")
    lines.append(f"  initial              : {metrics['initial_equity']:.2f}")
    lines.append(f"  final (realized)     : {metrics['final_equity_realized']:.2f}")
    lines.append(f"  final (mark-to-mkt)  : {metrics['final_equity_mtm']:.2f}")
    lines.append(f"  net return (mtm)     : {metrics['net_return_pct']:+.2f}%")
    lines.append(f"  max drawdown (mtm)   : {metrics['max_drawdown_pct']:.2f}%")
    lines.append("")

    lines.append("📈 Trades")
    lines.append(f"  total                : {metrics['total_trades']}")
    lines.append(
        f"  wins/losses          : {metrics['wins']} / {metrics['losses']}"
    )
    lines.append(f"  win rate             : {metrics['win_rate'] * 100:.1f}%")
    lines.append(f"  gross profit         : {metrics['gross_profit']:.2f}")
    lines.append(f"  gross loss           : {metrics['gross_loss']:.2f}")
    pf = metrics["profit_factor"]
    pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
    lines.append(f"  profit factor        : {pf_str}")
    lines.append(f"  avg win              : {metrics['avg_win']:.2f}")
    lines.append(f"  avg loss             : {metrics['avg_loss']:.2f}")
    lines.append(f"  expectancy           : {metrics['expectancy']:.4f}")
    lines.append("")

    lines.append("🎯 Risk-adjusted")
    lines.append(
        f"  sharpe_like_per_trade: {metrics['sharpe_like_per_trade']:.4f}"
    )
    calmar = metrics["calmar_like_simple"]
    calmar_str = f"{calmar:.2f}" if calmar != float("inf") else "inf"
    lines.append(f"  calmar_like_simple   : {calmar_str}")
    lines.append("")

    lines.append("📋 Composition")
    lines.append(f"  long entries         : {metrics['long_count']}")
    lines.append(f"  short entries        : {metrics['short_count']}")
    lines.append(f"  ambiguous exits      : {metrics['ambiguous_exit_count']}")
    lines.append("")

    lines.append("🚪 Exit reasons")
    for r, c in sorted(metrics["exit_reason_distribution"].items()):
        lines.append(f"  {r:15s}: {c}")
    lines.append("")

    lines.append("🔔 Event counts")
    for t, c in sorted(metrics["event_counts"].items()):
        lines.append(f"  {t:20s}: {c}")

    lines.append("=" * 60)
    return "\n".join(lines)


def result_to_json(result: dict) -> str:
    """سریال‌سازی نتیجه برای ذخیره."""
    out = {
        "symbol": result["symbol"],
        "candle_count": result["candle_count"],
        "start_index": result["start_index"],
        "initial_equity": result["initial_equity"],
        "metrics": result["metrics"],
        "strategy_metadata": result["strategy_metadata"],
        "event_count": len(result["events"]),
    }
    return json.dumps(out, indent=2, sort_keys=True, default=str)


# ============================================================
# CLI (local use only)
# ============================================================

def _cli_main():
    """اجرای محلی با داده واقعی Toobit. در CI استفاده نمی‌شود."""
    import sys
    from data_feed import fetch_closed_klines
    from symbol_specs import get_symbol_specs

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC-SWAP-USDT"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    print(f"🔍 Fetching {limit} closed candles for {symbol}...")
    klines = fetch_closed_klines(symbol, "1h", limit)
    print(f"✅ Received {len(klines)} candles")

    print(f"🔍 Fetching symbol specs...")
    specs = get_symbol_specs(symbol)
    print(f"✅ contract_multiplier = {specs['contract_multiplier']}")

    print("🚀 Running backtest...")
    result = run_backtest(
        klines, specs, initial_equity=1000.0, symbol=symbol,
    )

    print()
    print(format_metrics(result["metrics"]))
    print()
    print("📄 Strategy metadata:")
    print(json.dumps(result["strategy_metadata"], indent=2))


if __name__ == "__main__":
    _cli_main()