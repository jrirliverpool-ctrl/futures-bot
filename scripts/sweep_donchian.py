"""
sweep_donchian.py
Controlled comparison: Donchian 20 vs 15 vs 10.

Research-only:
- Does not modify strategy_config.py or operational state.
- Uses the same downloaded candles and execution settings.
- Does not automatically select a winning strategy.
"""

import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import backtest
from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs

SYMBOL = "BTC-SWAP-USDT"
TIMEFRAME = "1h"
INITIAL_EQUITY = 1000.0
WARMUP_CANDLES = 250
DONCHIAN_PERIODS = (20, 15, 10)

def utc_time(value):
    """Format a candle timestamp as UTC when possible."""
    try:
        ts = float(value)
        if ts > 10**12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)

def json_safe(value):
    """Avoid non-standard JSON Infinity and NaN values."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value

def fmt(value, pattern=".2f"):
    if isinstance(value, str):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else ("-Infinity" if value < 0 else "NaN")
    return format(value, pattern)

def main():
    requested_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    if requested_limit <= WARMUP_CANDLES:
        raise SystemExit(f"Requested limit must exceed {WARMUP_CANDLES}.")

    print(f"Symbol: {SYMBOL} | Timeframe: {TIMEFRAME}")
    print(f"Requested candles: {requested_limit}")
    print("Fetching closed candles once for all experiments...")

    klines = fetch_closed_klines(SYMBOL, TIMEFRAME, requested_limit)

    if not klines or len(klines) <= WARMUP_CANDLES:
        raise SystemExit(
            f"Insufficient candles. Received {len(klines) if klines else 0}; "
            f"need at least {WARMUP_CANDLES + 1}."
        )

    print(f"Actual candles received: {len(klines)}")
    print(f"First candle UTC: {utc_time(klines[0][0])}")
    print(f"Last candle UTC:  {utc_time(klines[-1][0])}")

    specs = get_symbol_specs(SYMBOL)
    original_get_params = backtest.get_indicator_params
    results = []

    for period in DONCHIAN_PERIODS:
        print(f"\nRunning Donchian {period}...")

        def test_get_params(p=period):
            params = dict(original_get_params())
            params["donchian_p"] = p
            return params

        # generate_signals() reads this imported function from backtest.
        # The patch is limited to this experiment and restored afterwards.
        with patch("backtest.get_indicator_params", side_effect=test_get_params):
            result = backtest.run_backtest(
                klines=klines,
                specs=specs,
                initial_equity=INITIAL_EQUITY,
                symbol=SYMBOL,
                start_index=WARMUP_CANDLES,
            )

        metrics = result["metrics"]
        final_state = result["final_state"]

        row = {
            "donchian_p_tested": period,
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "candle_count": len(klines),
            "first_candle_utc": utc_time(klines[0][0]),
            "last_candle_utc": utc_time(klines[-1][0]),
            "start_index": WARMUP_CANDLES,
            "initial_equity": INITIAL_EQUITY,
            "open_position_at_end": (final_state.get("position") is not None),
            "metrics": metrics,
        }
        results.append(row)

        print(
            f"Trades={metrics['total_trades']} | "
            f"Net return={fmt(metrics['net_return_pct'], '+.2f')}% | "
            f"PF={fmt(metrics['profit_factor'])} | "
            f"Max DD={fmt(metrics['max_drawdown_pct'])}% | "
            f"Expectancy={fmt(metrics['expectancy'], '.4f')}"
        )

    print("\n" + "=" * 112)
    print(f"{'METRIC':<24} | {'D20':>12} | {'D15':>12} | {'D10':>12}")
    print("-" * 112)

    metrics_to_print = [
        ("total_trades", "Closed trades", ".0f"),
        ("net_return_pct", "Net return (%)", "+.2f"),
        ("profit_factor", "Profit factor", ".3f"),
        ("max_drawdown_pct", "Max drawdown (%)", ".2f"),
        ("win_rate", "Win rate (%)", ".2%"),
        ("expectancy", "Expectancy (USD)", ".4f"),
        ("commission_cost_usd", "Commission (USD)", ".4f"),
    ]

    for key, label, pattern in metrics_to_print:
        values = []
        for item in results:
            value = item["metrics"][key]
            values.append(fmt(value, pattern))

        print(f"{label:<24} | {values[0]:>12} | {values[1]:>12} | {values[2]:>12}")

    print("=" * 112)
    print("\nResearch output only. No winner is selected automatically.")
    print("Verify funding, slippage, data gaps and open-position handling before interpreting profitability.")

    output_dir = os.path.join(REPO_DIR, "artifacts")
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, "donchian_sweep.json")
    csv_path = os.path.join(output_dir, "donchian_sweep.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_safe(results), f, indent=2, ensure_ascii=False)

    csv_fields = [
        "donchian_p_tested", "symbol", "timeframe", "candle_count",
        "first_candle_utc", "last_candle_utc", "start_index", "open_position_at_end",
        "total_trades", "wins", "losses", "net_return_pct", "profit_factor",
        "max_drawdown_pct", "win_rate", "expectancy", "commission_cost_usd",
        "final_equity_mtm", "final_equity_realized",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()

        for item in results:
            m = item["metrics"]
            writer.writerow({
                "donchian_p_tested": item["donchian_p_tested"],
                "symbol": item["symbol"],
                "timeframe": item["timeframe"],
                "candle_count": item["candle_count"],
                "first_candle_utc": item["first_candle_utc"],
                "last_candle_utc": item["last_candle_utc"],
                "start_index": item["start_index"],
                "open_position_at_end": item["open_position_at_end"],
                **{
                    key: (
                        str(json_safe(m.get(key)))
                        if isinstance(m.get(key), float) and not math.isfinite(m[key])
                        else m.get(key)
                    )
                    for key in csv_fields
                    if key in m
                },
            })

    print(f"\nSaved: {json_path}")
    print(f"Saved: {csv_path}")

if __name__ == "__main__":
    main()