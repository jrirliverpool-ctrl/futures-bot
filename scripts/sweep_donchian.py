"""
sweep_donchian.py — Safe Parameter Sweep for Donchian.
Compares Donchian 20, 15, 10 on the SAME data, with warmup excluded.
Does NOT auto-declare a winner.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs
import backtest

def main():
    symbol = "BTC-SWAP-USDT"
    limit = 1000
    initial_equity = 1000.0
    warmup = 250 # ✅ دوره گرم شدن اندیکاتورها

    print(f"🔍 Fetching {limit} closed candles for {symbol}...")
    klines = fetch_closed_klines(symbol, "1h", limit)
    print(f"✅ Received {len(klines)} candles (Warmup excluded: {warmup})\n")
    
    specs = get_symbol_specs(symbol)

    periods = [20, 15, 10]
    results = []

    for p in periods:
        print(f"🚀 Running backtest with Donchian period = {p}...")
        
        # ✅ تغییر موقت پارامتر Donchian فقط در ماژول backtest
        # این کار باعث می‌شود هش استراتژی اصلی (که در strategy_config است) تغییر نکند
        def mock_indicator_params():
            from strategy_config import get_indicator_params
            params = get_indicator_params()
            params["donchian_p"] = p
            return params

        with patch("backtest.get_indicator_params", side_effect=mock_indicator_params):
            result = backtest.run_backtest(
                klines, specs, initial_equity=initial_equity, symbol=symbol,
                start_index=warmup # ✅ نادیده گرفتن دوره گرم شدن
            )
            results.append((p, result["metrics"]))
        
        print(f"✅ Finished Donchian {p}\n")

    # ✅ چاپ جدول مقایسه (بدون انتخاب برنده)
    print("=" * 100)
    print(f"{'METRIC':<25} | {'Donchian 20':<20} | {'Donchian 15':<20} | {'Donchian 10':<20}")
    print("=" * 100)

    metrics_to_compare = [
        ("total_trades", "Total Trades", "{:d}"),
        ("net_return_pct", "Net Return %", "{:+.2f}%"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("max_drawdown_pct", "Max Drawdown %", "{:.2f}%"),
        ("win_rate", "Win Rate", "{:.1%}"),
        ("expectancy", "Expectancy USD", "{:.4f}"),
        ("commission_cost_usd", "Commission Cost", "{:.2f}"),
    ]

    for key, label, fmt in metrics_to_compare:
        row = f"{label:<25} | "
        for p, m in results:
            val = m.get(key)
            if val == float("inf"):
                row += f"{'inf':<20} | "
            else:
                row += f"{fmt.format(val):<20} | "
        print(row)
    
    print("=" * 100)
    print("\n📌 NOTE: This is a short-window test (1000 candles).")
    print("   Do NOT select a winner yet. Evaluate with longer data and out-of-sample.")
    print("   Also verify that Funding and Slippage are applied in the backtest engine.")

if __name__ == "__main__":
    main()