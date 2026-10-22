"""
sweep_e2.py — Controlled Comparison: Base vs Flexible (E2) across Donchian 20 & 15.
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
import strategy_flexible
from strategy import evaluate_signal as base_eval

def main():
    symbol = "BTC-SWAP-USDT"
    limit = 1000
    initial_equity = 1000.0
    warmup = 250

    print(f"🔍 Fetching {limit} closed candles...")
    klines = fetch_closed_klines(symbol, "1h", limit)
    specs = get_symbol_specs(symbol)

    scenarios = [
        ("Base_D20", 20, base_eval),
        ("Base_D15", 15, base_eval),
        ("Flex_D20", 20, strategy_flexible.evaluate_flexible_signal),
        ("Flex_D15", 15, strategy_flexible.evaluate_flexible_signal),
    ]

    results = []

    for name, p, eval_func in scenarios:
        print(f"🚀 Running {name}...")

        def mock_indicator_params(p=p):
            from strategy_config import get_indicator_params
            params = get_indicator_params()
            params["donchian_p"] = p
            return params

        with patch("backtest.get_indicator_params", side_effect=mock_indicator_params), \
             patch("backtest.evaluate_signal", side_effect=eval_func):
            result = backtest.run_backtest(
                klines, specs, initial_equity=initial_equity, symbol=symbol,
                start_index=warmup
            )
            results.append((name, result["metrics"]))

    print("\n" + "=" * 120)
    print(f"{'METRIC':<25} | {'Base_D20':<15} | {'Base_D15':<15} | {'Flex_D20':<15} | {'Flex_D15':<15}")
    print("=" * 120)

    metrics_to_compare = [
        ("total_trades", "Total Trades", "{:d}"),
        ("net_return_pct", "Net Return %", "{:+.2f}%"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("max_drawdown_pct", "Max DD %", "{:.2f}%"),
        ("win_rate", "Win Rate", "{:.1%}"),
        ("expectancy", "Expectancy USD", "{:.4f}"),
    ]

    for key, label, fmt in metrics_to_compare:
        row = f"{label:<25} | "
        for name, m in results:
            val = m.get(key)
            if val == float("inf"):
                row += f"{'inf':<15} | "
            else:
                row += f"{fmt.format(val):<15} | "
        print(row)

    print("=" * 120)
    print("\n📌 NOTE: E2 is strictly research. Do NOT push to main until OOS verified.")

if __name__ == "__main__":
    main()