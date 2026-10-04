"""
run_real_backtest.py — End-to-End Smoke Backtest روی داده‌ی واقعی Toobit.

هدف
════
این اسکریپت یک Smoke Backtest است، نه ارزیابی استراتژی.
هدف این است که مطمئن شویم زنجیره‌ی:

    Toobit Klines → Symbol Specs → Backtest → Performance
                  → CSV Export → State Save

بدون خطا و با اعداد معتبر عبور می‌کند.

معیار موفقیت = exit code 0 + اعداد معتبر، نه Sharpe بالا.

Audit Metadata
══════════════
قبل از ذخیره‌ی state، سه فیلد audit اضافه می‌شوند تا
audit_backtest_artifacts.py بتواند unrealized PnL را دقیقاً
محاسبه کند:

  symbol              : نماد بک‌تست شده
  contract_multiplier : multiplier نماد
  last_close_price    : close آخرین کندل (مبنای MTM نهایی)

این فیلدها در state_manager.validate_state اجباری نیستند
(فقط لازم برای audit دقیق).

متغیرهای محیطی
══════════════
  SYMBOL          : مثلاً BTC-SWAP-USDT
  INTERVAL        : مثلاً 1h
  LIMIT           : تعداد کندل (100..1000)
  INITIAL_EQUITY  : equity اولیه (USDT)

Spec: docs/strategy_v1.md
"""

import os
import sys

from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs
from backtest import run_backtest, format_metrics
from performance import build_performance_report
from state_manager import save_state


def main():
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")
    limit = int(os.environ.get("LIMIT", "500"))
    initial_equity = float(os.environ.get("INITIAL_EQUITY", "1000"))

    print("=" * 70)
    print("🚀 REAL TOOBIT SMOKE BACKTEST")
    print("=" * 70)
    print(f"  symbol         : {symbol}")
    print(f"  interval       : {interval}")
    print(f"  limit          : {limit}")
    print(f"  initial_equity : {initial_equity}")
    print("=" * 70)
    print()

    # ---- 1. Symbol specs ----
    print("📋 Fetching symbol specs...")
    try:
        specs = get_symbol_specs(symbol)
    except Exception as e:
        print(f"❌ symbol_specs failed: {e}")
        sys.exit(1)

    print(f"  contract_multiplier : {specs['contract_multiplier']}")
    print(f"  min_qty             : {specs['min_qty']}")
    print(f"  qty_step            : {specs['qty_step']}")
    print(f"  tick_size           : {specs['tick_size']}")
    print(f"  max_leverage        : {specs['max_leverage']}")
    print()

    # ---- 2. Klines ----
    print(f"📥 Fetching {limit} closed candles...")
    try:
        klines = fetch_closed_klines(symbol, interval, limit)
    except Exception as e:
        print(f"❌ data_feed failed: {e}")
        sys.exit(1)

    print(f"  received {len(klines)} closed candles")
    if len(klines) > 0:
        print(f"  first ts    : {int(klines[0][0])}")
        print(f"  last ts     : {int(klines[-1][0])}")
        print(f"  first close : {float(klines[0][4])}")
        print(f"  last close  : {float(klines[-1][4])}")
    print()

    if len(klines) < 250:
        print(
            f"❌ کندل کافی نیست: {len(klines)} (حداقل لازم: 250)"
        )
        sys.exit(1)

    # ---- 3. Backtest ----
    print("🧠 Running backtest...")
    try:
        result = run_backtest(
            klines, specs,
            initial_equity=initial_equity,
            symbol=symbol,
        )
    except Exception as e:
        print(f"❌ backtest failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print("✅ Backtest completed")
    print()

    # ---- 4. Metrics ----
    print(format_metrics(result["metrics"]))
    print()

    # ---- 5. Performance report ----
    print("📊 Building performance report...")
    try:
        perf = build_performance_report(
            result, klines,
            specs["contract_multiplier"],
            output_dir="reports",
        )
    except Exception as e:
        print(f"❌ performance failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print(f"  equity CSV : {perf['equity_csv']}")
    print(f"  trades CSV : {perf['trades_csv']}")
    print(f"  trades     : {len(perf['trades'])}")
    print()
    print(perf["report_text"])
    print()

    # ---- 6. Save state (with audit metadata) ----
    state_path = "backtest_state.json"

    final_state = result["final_state"]
    # Audit metadata — helps audit_backtest_artifacts.py compute
    # unrealized PnL exactly when a position is open at end.
    final_state["symbol"] = symbol
    final_state["contract_multiplier"] = specs["contract_multiplier"]
    final_state["last_close_price"] = float(klines[-1][4])

    try:
        save_state(state_path, final_state)
    except Exception as e:
        print(f"❌ save_state failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print(f"💾 Final state saved: {state_path}")
    print(f"  symbol              : {symbol}")
    print(f"  contract_multiplier : {specs['contract_multiplier']}")
    print(f"  last_close_price    : {float(klines[-1][4])}")
    print()

    # ---- 7. Summary ----
    print("=" * 70)
    print("📌 SMOKE BACKTEST SUMMARY")
    print("=" * 70)
    m = result["metrics"]
    print(f"  strategy_version : {result['strategy_metadata']['version']}")
    print(f"  strategy_hash    : {result['strategy_metadata']['hash']}")
    print(f"  candle_count     : {result['candle_count']}")
    print(f"  total_trades     : {m['total_trades']}")
    print(f"  net_return_pct   : {m['net_return_pct']:+.2f}%")
    print(f"  max_drawdown_pct : {m['max_drawdown_pct']:.2f}%")
    print(f"  win_rate         : {m['win_rate'] * 100:.1f}%")
    print(f"  final_equity     : {m['final_equity_mtm']:.2f}")
    print("=" * 70)
    print("✅ SMOKE BACKTEST PASSED")


if __name__ == "__main__":
    main()