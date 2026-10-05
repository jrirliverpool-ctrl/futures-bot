"""
run_paper.py — Paper Trading orchestrator برای ApexQuant.

هر اجرا:
  1. state را از STATE_DIR می‌خواند (یا init می‌کند)
  2. آخرین کندل بسته‌شده BTC/1h را از Toobit می‌گیرد
  3. اگر کندل جدید است (candle_ts > last_processed_candle_ts):
       a. indicators بساز
       b. strategy signal
       c. paper_engine.process_candle
       d. events را append کن
       e. state را save کن
  4. اگر کندل جدید نیست → skip

هیچ سفارش واقعی به Toobit ارسال نمی‌شود.
هیچ API خصوصی صدا زده نمی‌شود.

هیچ سفارشی به Exchange ارسال نمی‌شود. فقط شبیه‌سازی روی داده زنده.

متغیرهای محیطی:
  STATE_DIR : مسیر دایرکتوری paper-state (پیش‌فرض: ./paper_state_dir)
  SYMBOL    : نماد معاملاتی (پیش‌فرض: BTC-SWAP-USDT)
  INTERVAL  : تایم‌فریم (پیش‌فرض: 1h)

Spec: docs/step9a_paper_persistence.md
"""

import os
import sys

from paper_state_manager import (
    init_state,
    load_state,
    save_state,
    state_exists,
    append_events,
    load_events,
    events_count,
)
from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs
from indicators import compute_all
from strategy import evaluate_signal
from paper_engine import process_candle
from strategy_config import get_indicator_params, get_engine_config
from compute_hash import compute_strategy_hash


# ============================================================
# Helpers
# ============================================================

def _build_indicators(klines: list) -> dict:
    """compute_all با پارامترهای strategy_config."""
    p = get_indicator_params()
    return compute_all(
        klines,
        ema_fast_p=p["ema_fast_p"],
        ema_slow_p=p["ema_slow_p"],
        donchian_p=p["donchian_p"],
        atr_p=p["atr_p"],
    )


def _enrich_specs_for_engine(specs: dict) -> dict:
    """specs را به فرمت engine config تبدیل می‌کند."""
    cfg = get_engine_config()
    cfg["contract_multiplier_for_pnl"] = specs["contract_multiplier"]
    return cfg


def _detect_new_candle(klines: list, last_processed_ts) -> tuple:
    """
    آیا کندل جدیدی هست که پردازش نشده؟

    Returns:
        (should_process: bool, index_to_process: int or None)
    """
    if not klines:
        return False, None

    # آخرین کندل دریافت‌شده
    last_ts = int(klines[-1][0])

    # اگر آخرین کندل قبلاً پردازش شده → کاری نیست
    if last_processed_ts is not None and last_ts <= last_processed_ts:
        return False, None

    # index آخرین کندل
    return True, len(klines) - 1


# ============================================================
# Main
# ============================================================

def main():
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")

    print("=" * 70)
    print("📄 PAPER TRADING RUN")
    print("=" * 70)
    print(f"  STATE_DIR : {state_dir}")
    print(f"  SYMBOL    : {symbol}")
    print(f"  INTERVAL  : {interval}")
    print(f"  strategy  : {compute_strategy_hash()}")
    print("=" * 70)
    print()

    # ---- 1. State: load or init ----
    was_existing = state_exists(state_dir)
    if was_existing:
        state = load_state(state_dir)
        print(f"✅ State loaded (existing)")
    else:
        state = init_state(state_dir, initial_equity=1000.0, symbol=symbol)
        print(f"✅ State initialized")

    last_processed = state.get("last_processed_candle_ts")
    print(f"  last_processed_candle_ts : {last_processed}")
    print(f"  equity                   : {state['equity']}")
    print(f"  peak_equity              : {state['peak_equity']}")
    print(f"  has_position             : {state['position'] is not None}")
    print()

    # ---- 2. Fetch symbol specs ----
    print(f"📋 Fetching symbol specs...")
    try:
        specs = get_symbol_specs(symbol)
    except Exception as e:
        print(f"❌ symbol_specs failed: {e}")
        sys.exit(1)
    print(f"  contract_multiplier : {specs['contract_multiplier']}")
    print()

    # ---- 3. Fetch closed klines ----
    print(f"📥 Fetching closed {interval} candles...")
    try:
        klines = fetch_closed_klines(symbol, interval, limit=300)
    except Exception as e:
        print(f"❌ data_feed failed: {e}")
        sys.exit(1)

    print(f"  received {len(klines)} closed candles")
    if len(klines) > 0:
        print(f"  last candle ts : {int(klines[-1][0])}")
        print(f"  last close     : {float(klines[-1][4])}")
    print()

    if len(klines) < 250:
        print(f"⚠️  کندل کافی نیست ({len(klines)} < 250) — exit")
        sys.exit(0)

    # ---- 4. Detect new candle ----
    should_process, idx = _detect_new_candle(klines, last_processed)

    if not should_process:
        print(f"⏭  No new closed candle. Last processed = {last_processed}")
        print(f"   Latest available    = {int(klines[-1][0])}")
        print()
        print("=" * 70)
        print("✅ PAPER RUN — No-op (no new candle)")
        print("=" * 70)
        sys.exit(0)

    new_candle_ts = int(klines[idx][0])
    print(f"🆕 New candle detected: {new_candle_ts}")
    print()

    # ---- 5. Compute indicators ----
    print("🧠 Computing indicators...")
    try:
        indicators = _build_indicators(klines)
    except Exception as e:
        print(f"❌ indicators failed: {e}")
        sys.exit(1)
    print(f"  ✅ computed")

    # ---- 6. Strategy signal ----
    sig = evaluate_signal(indicators, idx)
    print(f"📊 Signal: {sig['signal']}")
    print(f"  reason : {sig.get('reason', '')}")
    print()

    # ---- 7. Process candle through paper_engine ----
    engine_config = _enrich_specs_for_engine(specs)

    print("⚙️  Processing through paper_engine...")
    try:
        state, events = process_candle(
            state,
            klines,
            indicators,
            idx,
            specs,
            engine_config,
            signal=sig,
        )
    except Exception as e:
        print(f"❌ paper_engine failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print(f"  events generated: {len(events)}")
    for ev in events:
        print(f"    [{ev.get('type')}] "
              f"side={ev.get('side')} price={ev.get('price')} "
              f"reason={ev.get('reason')}")
    print()

    # ---- 8. Append events ----
    if events:
        n_app, n_sk = append_events(state_dir, events)
        print(f"📝 Events appended: {n_app}, skipped: {n_sk}")
    else:
        print(f"📝 No events to append")
    print()

    # ---- 9. Update state ----
    state["last_processed_candle_ts"] = new_candle_ts
    state["symbol"] = symbol
    state["contract_multiplier"] = specs["contract_multiplier"]
    state["last_close_price"] = float(klines[idx][4])

    try:
        save_state(state_dir, state)
    except Exception as e:
        print(f"❌ save_state failed: {e}")
        sys.exit(1)
    print(f"✅ State saved")
    print()

    # ---- 10. Summary ----
    total_events = events_count(state_dir)

    print("=" * 70)
    print("📌 PAPER RUN SUMMARY")
    print("=" * 70)
    print(f"  candle_ts          : {new_candle_ts}")
    print(f"  signal             : {sig['signal']}")
    print(f"  events_generated   : {len(events)}")
    print(f"  total_events       : {total_events}")
    print(f"  equity             : {state['equity']}")
    print(f"  peak_equity        : {state['peak_equity']}")
    print(f"  has_position       : {state['position'] is not None}")
    print(f"  daily_loss         : {state['daily_loss']}")
    print(f"  weekly_loss        : {state['weekly_loss']}")
    print(f"  circuit_breaker    : {state['circuit_breaker_active']}")
    print("=" * 70)
    print("✅ PAPER RUN COMPLETED")


if __name__ == "__main__":
    main()