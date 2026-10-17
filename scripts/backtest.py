"""
backtest.py — Historical backtest engine برای ApexQuant V1.

v2.1 (B1 — Commission):
  - run_backtest از get_fee_config() استفاده می‌کند (fail-loud)
  - compute_metrics فیلد commission_cost_usd اضافه شد
"""

import json

from indicators import compute_all
from strategy import evaluate_signal
from paper_engine import process_candle
from strategy_config import (
    get_indicator_params,
    get_engine_config,
    get_fee_config,
)
from compute_hash import get_strategy_metadata
from state_manager import default_state


def compute_mtm_equity(state, close_price, specs):
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


def generate_signals(klines):
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


def run_backtest(klines, specs, initial_equity=1000.0,
                 symbol="UNKNOWN", start_index=0):
    if not klines:
        raise ValueError("klines is empty")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    indicators, signals = generate_signals(klines)
    state = default_state(initial_equity)

    engine_config = get_engine_config()
    engine_config["contract_multiplier_for_pnl"] = specs["contract_multiplier"]

    # B1: fail-loud — اگر get_fee_config fail شد، exception بالا می‌رود
    fee_cfg = get_fee_config()
    engine_config["open_taker_fee"] = fee_cfg["open_taker_fee"]
    engine_config["close_taker_fee"] = fee_cfg["close_taker_fee"]

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

        close_price = float(klines[i][4])
        mtm = compute_mtm_equity(state, close_price, specs)
        mtm_curve.append(mtm)
        realized_curve.append(state["equity"])

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


def compute_metrics(equity_curve, events, initial_equity,
                    realized_final=None):
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    mtm_final = equity_curve[-1] if equity_curve else initial_equity
    if realized_final is None:
        realized_final = mtm_final

    net_return_pct = (mtm_final - initial_equity) / initial_equity * 100.0

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

    exit_reasons = {}
    for e in exit_events:
        r = e.get("reason", "UNKNOWN")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    entry_events = [e for e in events if e.get("type") == "ENTRY"]
    long_count = sum(1 for e in entry_events if e.get("side") == "LONG")
    short_count = sum(1 for e in entry_events if e.get("side") == "SHORT")

    ambiguous_count = sum(1 for e in exit_events if e.get("ambiguous"))

    if len(trade_pnls) >= 2:
        mean_pnl = sum(trade_pnls) / len(trade_pnls)
        variance = sum(
            (p - mean_pnl) ** 2 for p in trade_pnls
        ) / (len(trade_pnls) - 1)
        std = variance ** 0.5
        sharpe_like_per_trade = mean_pnl / std if std > 0 else 0.0
    else:
        sharpe_like_per_trade = 0.0

    if max_dd_pct > 0:
        calmar_like_simple = net_return_pct / max_dd_pct
    else:
        calmar_like_simple = (
            float("inf") if net_return_pct > 0 else 0.0
        )

    event_counts = {}
    for e in events:
        t = e.get("type", "UNKNOWN")
        event_counts[t] = event_counts.get(t, 0) + 1

    # B1: commission_cost_usd
    commission_cost_usd = sum(
        float(e.get("commission_usd", 0.0) or 0.0)
        for e in events
        if e.get("type") in ("ENTRY", "TP1_HIT", "EXIT")
    )

    return {
        "initial_equity": float(initial_equity),
        "final_equity_mtm": float(mtm_final),
        "final_equity_realized": float(realized_final),
        "net_return_pct": float(net_return_pct),
        "max_drawdown_pct": float(max_dd_pct),
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
        "sharpe_like_per_trade": float(sharpe_like_per_trade),
        "calmar_like_simple": float(calmar_like_simple),
        "long_count": long_count,
        "short_count": short_count,
        "ambiguous_exit_count": ambiguous_count,
        "exit_reason_distribution": exit_reasons,
        "event_counts": event_counts,
        "total_events": len(events),
        "commission_cost_usd": float(commission_cost_usd),
    }


def format_metrics(metrics):
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
    lines.append(f"  commission cost      : {metrics.get('commission_cost_usd', 0):.4f}")
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


def result_to_json(result):
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


def _cli_main():
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