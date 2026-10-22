"""
sweep_oos.py — Rigorous OOS with Data Validation and Equal Windows.
"""
import os
import sys
import requests
import time
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backtest
import strategy_flexible
from strategy import evaluate_signal as base_eval
from symbol_specs import get_symbol_specs

SYMBOL = "BTC-SWAP-USDT"
TIMEFRAME = "1h"
INITIAL_EQUITY = 1000.0
WARMUP = 250

def fetch_klines_vision(limit=4000):
    print(f"🌐 Fetching {limit} candles from Binance Data Vision...")
    url = "https://data-api.binance.vision/api/v3/klines"
    all_klines = []
    end_time = None
    remaining = limit
    while remaining > 0:
        fetch_lim = min(remaining, 1000)
        params = {"symbol": "BTCUSDT", "interval": "1h", "limit": fetch_lim}
        if end_time: params["endTime"] = end_time
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data: break
        for k in data:
            all_klines.append([int(k[0]), k[1], k[2], k[3], k[4], k[5]])
        end_time = int(data[0][0]) - 1
        remaining -= len(data)
        time.sleep(0.1)
    all_klines.sort(key=lambda x: x[0])
    
    # ✅ اعتبارسنجی سخت‌گیرانه داده‌ها
    timestamps = [k[0] for k in all_klines]
    if len(timestamps) != len(set(timestamps)):
        raise SystemExit("❌ Duplicate candle timestamps detected.")
    if timestamps != sorted(timestamps):
        raise SystemExit("❌ Candle timestamps are not sorted.")
    for prev_ts, curr_ts in zip(timestamps, timestamps[1:]):
        if curr_ts - prev_ts != 3_600_000:
            raise SystemExit(f"❌ Missing or irregular 1h candle: {prev_ts} -> {curr_ts}")
            
    return all_klines

def main():
    klines = fetch_klines_vision(limit=4000)
    if len(klines) < 4000:
        raise SystemExit(f"❌ Failed to fetch 4000 candles. Got {len(klines)}")
        
    # ✅ برابری طول دوره‌ها (هر دو 1000 کندل ارزیابی) با Gap
    # IS: 1250 کندل (250 Warmup + 1000 Eval)
    # Gap: 1500 کندل (1250 تا 2750)
    # OOS: 1250 کندل (250 Warmup + 1000 Eval)
    klines_is = klines[:1250]
    klines_oos_with_warmup = klines[2750:4000]
    
    ts_is_start = datetime.fromtimestamp(klines_is[0][0]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    ts_is_end = datetime.fromtimestamp(klines_is[-1][0]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    ts_oos_start = datetime.fromtimestamp(klines_oos_with_warmup[0][0]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    ts_oos_end = datetime.fromtimestamp(klines_oos_with_warmup[-1][0]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    
    print(f"\n📊 Data Split Verification:")
    print(f"   IS Period:  {ts_is_start} to {ts_is_end} (Eval: 1000 candles)")
    print(f"   Gap:        {(datetime.fromtimestamp(klines[1250][0]/1000, tz=timezone.utc)).strftime('%Y-%m-%d')} to {(datetime.fromtimestamp(klines[2749][0]/1000, tz=timezone.utc)).strftime('%Y-%m-%d')}")
    print(f"   OOS Period: {ts_oos_start} to {ts_oos_end} (Eval: 1000 candles)")
    
    print("\n⚠️ WARNING: This uses Spot BTCUSDT as a proxy for Perp BTC-SWAP-USDT.")
    print("   Results are indicative, not definitive for Toobit execution.\n")
    
    specs = get_symbol_specs(SYMBOL)
    specs["contract_multiplier"] = 0.001 

    scenarios = [
        ("Base_D20", 20, base_eval),
        ("Base_D15", 15, base_eval),
        ("Flex_D20", 20, strategy_flexible.evaluate_flexible_signal),
        ("Flex_D15", 15, strategy_flexible.evaluate_flexible_signal),
    ]

    def run_scenario(klines_data, p, eval_func):
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

    print("🚀 RUNNING IN-SAMPLE (1000 eval candles)...")
    for name, p, func in scenarios:
        is_results[name] = run_scenario(klines_is, p, func)

    print("🚀 RUNNING OUT-OF-SAMPLE (1000 eval candles)...")
    for name, p, func in scenarios:
        oos_results[name] = run_scenario(klines_oos_with_warmup, p, func)

    print("\n" + "="*120)
    print(f"{'METRIC':<20} | {'Base_D20 (IS)':<15} | {'Base_D20 (OOS)':<15} | {'Flex_D15 (IS)':<15} | {'Flex_D15 (OOS)':<15}")
    print("="*120)
    
    for key, label, fmt in [
        ("total_trades", "Total Trades", "{:d}"),
        ("net_return_pct", "Net Return %", "{:+.2f}%"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("max_drawdown_pct", "Max DD %", "{:.2f}%"),
        ("win_rate", "Win Rate", "{:.1%}"),
        ("expectancy", "Expectancy USD", "{:.4f}"),
    ]:
        row = f"{label:<20} | "
        row += f"{fmt.format(is_results['Base_D20'].get(key)):<15} | "
        row += f"{fmt.format(oos_results['Base_D20'].get(key)):<15} | "
        row += f"{fmt.format(is_results['Flex_D15'].get(key)):<15} | "
        row += f"{fmt.format(oos_results['Flex_D15'].get(key)):<15} | "
        print(row)
    print("="*120)

if __name__ == "__main__":
    main()