"""
run_real_backtest.py — CLI entry point for running a smoke backtest against real Toobit data.
Generates: reports/trades.csv, reports/equity_curve.csv, and prints Summary.
"""

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs
from backtest import run_backtest, format_metrics


def generate_trades_csv(events, klines, specs, filepath):
    """Extracts entry/exit pairs from events and writes to trades.csv with full accounting details."""
    trades = []
    mult = specs.get("contract_multiplier", 1.0)
    
    entry_event = None
    for e in events:
        if e.get("type") == "ENTRY":
            entry_event = e
        elif e.get("type") == "EXIT" and entry_event is not None:
            entry_price = entry_event["price"]
            exit_price = e["exit_price_adj"]
            size = entry_event["size"]
            
            initial_sl = entry_event["sl"]
            risk_dist = abs(entry_price - initial_sl)
            
            # ✅ FIX 2: Use contract multiplier for exact Risk and R-multiple
            risk_usd = risk_dist * size * mult
            pnl_gross = e.get("pnl_usd", 0.0)
            pnl_net = e.get("trade_net_pnl_usd", pnl_gross)
            r_multiple = pnl_gross / risk_usd if risk_usd > 0 else 0.0
            
            # ✅ FIX 3: Extract exact timestamps from klines
            entry_ts = klines[entry_event["candle_index"]][0] if entry_event.get("candle_index") is not None else ""
            exit_ts = klines[e["candle_index"]][0] if e.get("candle_index") is not None else ""
            
            trades.append({
                "trade_index": len(trades),
                "side": entry_event["side"],
                "entry_ts_ms": entry_ts,
                "exit_ts_ms": exit_ts,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size": size,
                "pnl_usd": pnl_gross,
                "pnl_net": pnl_net,
                "commission_usd": e.get("total_commission_usd", 0.0),
                "risk_usd": risk_usd,
                "r_multiple": r_multiple,
                "exit_reason": e["reason"],
                "ambiguous_exit": e.get("ambiguous", False),
                "candles_held": e["candle_index"] - entry_event["candle_index"] if e.get("candle_index") is not None and entry_event.get("candle_index") is not None else 0
            })
            entry_event = None

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "trade_index", "side", "entry_ts_ms", "exit_ts_ms", 
            "entry_price", "exit_price", "size", 
            "pnl_usd", "pnl_net", "commission_usd", 
            "risk_usd", "r_multiple", "exit_reason", "ambiguous_exit", "candles_held"
        ])
        writer.writeheader()
        writer.writerows(trades)
    return len(trades)


def generate_equity_curve_csv(result, klines, filepath):
    """Writes the MTM and Realized equity curves to a CSV."""
    mtm = result["equity_curve"]
    realized = result["realized_equity_curve"]
    
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["candle_index", "candle_ts_ms", "candle_ts_iso", "equity_mtm", "equity_realized", "drawdown_pct", "peak_equity"])
        
        peak = result["initial_equity"]
        for i in range(len(mtm)):
            eq_mtm = mtm[i]
            eq_real = realized[i]
            if eq_mtm > peak: peak = eq_mtm
            dd = (peak - eq_mtm) / peak * 100 if peak > 0 else 0.0
            
            # ✅ FIX 3: Extract actual timestamps
            ts_ms = klines[i][0] if i < len(klines) else ""
            ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat() if ts_ms != "" else ""
            
            writer.writerow([i, ts_ms, ts_iso, eq_mtm, eq_real, dd, peak])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC-SWAP-USDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--initial_equity", type=float, default=1000.0)
    args = parser.parse_args()

    print("=" * 70)
    print("🚀 REAL TOOBIT SMOKE BACKTEST (ACCOUNTING AUDIT)")
    print("=" * 70)
    print(f"  symbol         : {args.symbol}")
    print(f"  interval       : {args.interval}")
    print(f"  limit          : {args.limit}")
    print(f"  initial_equity : {args.initial_equity}")
    print("=" * 70)

    print("\n📋 Fetching symbol specs...")
    specs = get_symbol_specs(args.symbol)
    print(f"  contract_multiplier : {specs.get('contract_multiplier')}")

    print(f"\n📥 Fetching {args.limit} closed candles...")
    klines = fetch_closed_klines(args.symbol, args.interval, args.limit)
    print(f"  received {len(klines)} closed candles")

    print("\n🧠 Running backtest...")
    result = run_backtest(klines, specs, initial_equity=args.initial_equity, symbol=args.symbol)
    print("✅ Backtest completed\n")

    # Print Metrics to stdout
    print(format_metrics(result["metrics"]))
    
    # Generate CSVs
    trades_path = f"backtest-out/{args.symbol}_trades.csv"
    equity_path = f"backtest-out/{args.symbol}_equity_curve.csv"
    
    num_trades = generate_trades_csv(result["events"], klines, specs, trades_path)
    generate_equity_curve_csv(result, klines, equity_path)
    
    print(f"\n💾 Saved {num_trades} trades to {trades_path}")
    print(f"\n💾 Saved equity curve to {equity_path}")

if __name__ == "__main__":
    main()