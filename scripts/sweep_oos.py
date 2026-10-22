"""
sweep_oos.py — OOS Validation v2.3.
- None-safe formatting (fail-soft, no crash)
- Explicitly reports missing metrics
- Only uses fields currently available in backtest.py
- Funding and open-position reporting will be added after separate audit
"""
import os
import sys
import time
from unittest.mock import patch
from datetime import datetime, timezone
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backtest
import strategy_flexible
from strategy import evaluate_signal as base_eval
from symbol_specs import get_symbol_specs

SYMBOL = "BTC-SWAP-USDT"
INITIAL_EQUITY = 1000.0
WARMUP = 250
INTERVAL_MS = 3_600_000


def fetch_klines_vision(limit=4100):
    print(f"🌐 Fetching up to {limit} candles from Binance Data Vision...")
    url = "https://data-api.binance.vision/api/v3/klines"
    all_klines = []
    end_time = None
    remaining = limit

    while remaining > 0:
        fetch_lim = min(remaining, 1000)
        params = {"symbol": "BTCUSDT", "interval": "1h", "limit": fetch_lim}
        if end_time:
            params["endTime"] = end_time
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        for k in data:
            all_klines.append([int(k[0]), k[1], k[2], k[3], k[4], k[5]])
        end_time = int(data[0][0]) - 1
        remaining -= len(data)
        time.sleep(0.1)

    all_klines.sort(key=lambda x: x[0])
    now_ms = int(time.time() * 1000)
    closed = [k for k in all_klines if k[0] + INTERVAL_MS <= now_ms]
    print(f"   Total fetched: {len(all_klines)}, closed: {len(closed)}")

    if len(closed) < 4000:
        raise SystemExit(f"❌ Need 4000 closed candles, got {len(closed)}")

    closed = closed[-4000:]
    timestamps = [k[0] for k in closed]
    if len(timestamps) != len(set(timestamps)):
        raise SystemExit("❌ Duplicate timestamps")
    for prev_ts, curr_ts in zip(timestamps, timestamps[1:]):
        if curr_ts - prev_ts != INTERVAL_MS:
            raise SystemExit(f"❌ Irregular candle: {prev_ts} -> {curr_ts}")

    return closed


def _fmt(v, fmt):
    """Format metric; return N/A if None, but note it in the missing report."""
    if v is None:
        return "N/A"
    if isinstance(v, str):
        return v
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def print_table(title, results, scenarios):
    print(f"\n=== {title} ===")
    header = f"{'METRIC':<22} | " + " | ".join(f"{s:<12}" for s in scenarios)
    print(header)
    print("-" * len(header))

    metrics = [
        ("total_trades", "Total Trades", "{:d}"),
        ("net_return_pct", "Net Return %", "{:+.2f}%"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("max_drawdown_pct", "Max DD %", "{:.2f}%"),
        ("win_rate", "Win Rate", "{:.1%}"),
        ("expectancy", "Expectancy USD", "{:.4f}"),
        ("commission_cost_usd", "Commissions $", "{:.2f}"),
    ]

    missing_keys = set()
    for key, label, fmt in metrics:
        row = f"{label:<22} | "
        for s in scenarios:
            v = results[s].get(key)
            if v is None:
                missing_keys.add(key)
            row += f"{_fmt(v, fmt):<12} | "
        print(row)

    # ✅ Explicit report of missing metrics
    if missing_keys:
        print(f"\n⚠️ MISSING METRICS (not yet implemented in backtest.py):")
        for k in sorted(missing_keys):
            print(f"   - {k}")


def main():
    klines = fetch_klines_vision(limit=4100)
    if len(klines) < 4000:
        raise SystemExit(f"❌ Need 4000 closed candles, got {len(klines)}")

    klines_is = klines[:1250]
    klines_oos_with_warmup = klines[2750:4000]

    ts_is_start = datetime.fromtimestamp(klines_is[0][0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
    ts_is_end = datetime.fromtimestamp(klines_is[-1][0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
    ts_oos_start = datetime.fromtimestamp(klines_oos_with_warmup[0][0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
    ts_oos_end = datetime.fromtimestamp(klines_oos_with_warmup[-1][0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')

    print(f"\n📊 Data Split:")
    print(f"   IS:  {ts_is_start} → {ts_is_end} (1000 eval)")
    print(f"   OOS: {ts_oos_start} → {ts_oos_end} (1000 eval)")

    specs = get_symbol_specs(SYMBOL)
    specs["contract_multiplier"] = 0.001

    scenarios = [
        ("Base_D20", 20, base_eval),
        ("Base_D15", 15, base_eval),
        ("Flex_D20", 20, strategy_flexible.evaluate_flexible_signal),
        ("Flex_D15", 15, strategy_flexible.evaluate_flexible_signal),
    ]

    def run_scenario(klines_data, p, eval_func):
        def mock_params(p=p):
            from strategy_config import get_indicator_params
            params = get_indicator_params()
            params["donchian_p"] = p
            return params
        with patch("backtest.get_indicator_params", side_effect=mock_params), \
             patch("backtest.evaluate_signal", side_effect=eval_func):
            return backtest.run_backtest(
                klines_data, specs, initial_equity=INITIAL_EQUITY,
                symbol=SYMBOL, start_index=WARMUP
            )["metrics"]

    is_results = {}
    oos_results = {}

    print("\n🚀 Running IS...")
    for name, p, func in scenarios:
        is_results[name] = run_scenario(klines_is, p, func)

    print("🚀 Running OOS...")
    for name, p, func in scenarios:
        oos_results[name] = run_scenario(klines_oos_with_warmup, p, func)

    scenario_names = [s[0] for s in scenarios]
    print_table("IN-SAMPLE", is_results, scenario_names)
    print_table("OUT-OF-SAMPLE", oos_results, scenario_names)

    print("\n⚠️ Data: Binance Spot BTCUSDT (proxy for Toobit Perp)")
    print("⚠️ This is RESEARCH. Do NOT promote to production without OOS approval.")
    print("⚠️ Funding and open-position reporting will be added in a separate step.")


if __name__ == "__main__":
    main()