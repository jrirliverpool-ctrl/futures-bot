"""
sweep_oos.py — Rigorous Out-of-Sample (OOS) Test.
Uses Binance Data Vision (data-api.binance.vision) to bypass geo-blocks.
"""
import os
import sys
import requests
import time
from unittest.mock import patch

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

import backtest
import strategy_flexible
from strategy import evaluate_signal as base_eval
from symbol_specs import get_symbol_specs

SYMBOL = "BTC-SWAP-USDT"
TIMEFRAME = "1h"
INITIAL_EQUITY = 1000.0
WARMUP = 250

def fetch_klines_vision(limit=2000):
    """Fetches historical candles from Binance Data Vision API."""
    print(f"🌐 Fetching {limit} candles from Binance Data Vision...")
    url = "https://data-api.binance.vision/api/v3/klines"
    
    all_klines = []
    end_time = None
    
    remaining = limit
    while remaining > 0:
        fetch_lim = min(remaining, 1000)
        params = {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "limit": fetch_lim
        }
        if end_time:
            params["endTime"] = end_time
            
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        if not data:
            break
            
        # Convert to ApexQuant format: [ts, open, high, low, close, volume]
        for k in data:
            all_klines.append([int(k[0]), k[1], k[2], k[3], k[4], k[5]])
            
        # Update end_time to before the earliest fetched candle
        end_time = int(data[0][0]) - 1
        remaining -= len(data)
        time.sleep(0.1) # Rate limit
        
    # Sort ascending
    all_klines.sort(key=lambda x: x[0])
    return all_klines

def main():
    # 1. Fetch Data
    klines = fetch_klines_vision(limit=2000)
    if len(klines) < 2000:
        raise SystemExit(f"❌ Failed to fetch enough data. Got {len(klines)}")
        
    print(f"✅ Fetched {len(klines)} candles total.")
    
    # 2. Split Data
    klines_is = klines[:1000]
    # OOS slice includes 250 warmup candles from the IS period
    klines_oos_with_warmup = klines[750:2000]
    
    print(f"   IS candles: {len(klines_is)} (Warmup: {WARMUP})")
    print(f"   OOS candles: {len(klines_oos_with_warmup)} (Warmup: {WARMUP})")
    
    specs = get_symbol_specs(SYMBOL)
    specs["contract_multiplier"] = 0.001 # Binance BTCUSDT linear equivalent

    scenarios = [
        ("Base_D20", 20, base_eval),
        ("Base_D15", 15, base_eval),
        ("Flex_D20", 20, strategy_flexible.evaluate_flexible_signal),
        ("Flex_D15", 15, strategy_flexible.evaluate_flexible_signal),
    ]

    def run_scenario(klines_data, p, eval_func, label):
        def mock_indicator_params(p=p):
            from strategy_config import get_indicator_params
            params = get_indicator_params()
            params["donchian_p"] = p
            return params

        with patch("backtest.get_indicator_params", side_effect=mock_indicator_params), \
             patch("backtest.evaluate_signal", side_effect=eval_func):
            result = backtest.run_backtest(
                klines_data, specs, initial_equity=INITIAL_EQUITY, symbol=SYMBOL,
                start_index=WARMUP
            )
        return result["metrics"]

    is_results = {}
    oos_results = {}

    # 3. Run IS
    print("\n" + "="*80)
    print("🚀 RUNNING IN-SAMPLE (1000 candles)")
    print("="*80)
    for name, p, func in scenarios:
        print(f"   Running {name}...")
        is_results[name] = run_scenario(klines_is, p, func, "IS")

    # 4. Run OOS
    print("\n" + "="*80)
    print("🚀 RUNNING OUT-OF-SAMPLE (1000 unseen candles)")
    print("="*80)
    for name, p, func in scenarios:
        print(f"   Running {name}...")
        oos_results[name] = run_scenario(klines_oos_with_warmup, p, func, "OOS")

    # 5. Print Results
    print("\n" + "="*120)
    print(f"{'METRIC':<20} | {'Base_D20 (IS)':<15} | {'Base_D20 (OOS)':<15} | {'Flex_D15 (IS)':<15} | {'Flex_D15 (OOS)':<15}")
    print("="*120)

    metrics_to_compare = [
        ("total_trades", "Total Trades", "{:d}"),
        ("net_return_pct", "Net Return %", "{:+.2f}%"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("max_drawdown_pct", "Max DD %", "{:.2f}%"),
        ("win_rate", "Win Rate", "{:.1%}"),
        ("expectancy", "Expectancy USD", "{:.4f}"),
    ]

    for key, label, fmt in metrics_to_compare:
        row = f"{label:<20} | "
        
        v = is_results["Base_D20"].get(key)
        row += f"{fmt.format(v):<15} | "
        v = oos_results["Base_D20"].get(key)
        row += f"{fmt.format(v):<15} | "
        v = is_results["Flex_D15"].get(key)
        row += f"{fmt.format(v):<15} | "
        v = oos_results["Flex_D15"].get(key)
        row += f"{fmt.format(v):<15} | "
        
        print(row)
        
    print("="*120)
    print("\n📌 NOTE: Only if Flex_D15 outperforms Base_D20 in the OOS column,")
    print("   we can consider moving to the next phase. Still NO auto-trading.")

if __name__ == "__main__":
    main()