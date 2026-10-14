"""
run_paper.py — Paper Trading orchestrator برای ApexQuant.

هر اجرا:
  1. state را از STATE_DIR می‌خواند (یا init می‌کند)
  2. آخرین کندل بسته‌شده را از Toobit می‌گیرد
  3. اگر کندل جدید است:
       a. indicators بساز
       b. برای هر کندل جدید (به‌ترتیب صعودی، حداکثر MAX_CATCHUP_CANDLES):
          i.   strategy signal
          ii.  Kill Switch check (fail-closed)
          iii. paper_engine.process_candle
          iv.  events را append کن
          v.   state را save کن
  4. اگر کندل جدید نیست → skip
  5. اگر بیش از MAX_CATCHUP_CANDLES عقب‌ماندگی باشد، فقط قدیمی‌ترین‌ها
     پردازش می‌شوند و مقدار lag در state و خروجی گزارش می‌شود.
     اجرای بعدی، باقی‌مانده را پردازش می‌کند (self-healing).

Kill Switch:
  - پیش از پردازش، is_kill_switch_active() صدا زده می‌شود.
  - نتیجه در engine_config تزریق می‌شود:
      kill_switch_active, kill_switch_reason
  - fail-closed:
      * خطای بررسی → active=True
      * خروجی نامعتبر → active=True
      * active non-bool → active=True
  - خود موتور (paper_engine) فقط این دو فیلد را می‌خواند.

هیچ سفارش واقعی به صرافی ارسال نمی‌شود.

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
from strategy_config import (
    get_indicator_params,
    get_engine_config,
)
from compute_hash import compute_strategy_hash
from kill_switch import is_kill_switch_active


# ============================================================
# Constants
# ============================================================

# حداکثر تعداد کندل‌هایی که در یک اجرا پردازش می‌شوند.
# اگر عقب‌ماندگی بیشتر باشد، باقی در اجرای بعدی پردازش می‌شود.
MAX_CATCHUP_CANDLES = 100


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
    """
    engine config از strategy_config + injection.

    get_engine_config() شامل:
      leverage, leverage_cap, risk_per_trade, ...

    اینجا فقط contract_multiplier_for_pnl اضافه می‌شود.
    """
    cfg = get_engine_config()
    cfg["contract_multiplier_for_pnl"] = specs["contract_multiplier"]
    return cfg


def _build_engine_config_with_kill_switch(specs, state, state_dir,
                                           repo_root=None):
    """
    ساخت engine config + تزریق وضعیت Kill Switch.

    Fail-closed:
      - خطای is_kill_switch_active → active=True
      - خروجی غیر tuple یا با طول ≠ 2 → active=True
      - active non-bool → active=True
      - active=True و reason=None → reason پیش‌فرض
      - active=False → reason=None
    """
    cfg = _enrich_specs_for_engine(specs)

    ks_active = True
    ks_reason = "fail-closed: kill_switch check did not complete"

    try:
        result = is_kill_switch_active(
            state_dir, state=state, repo_root=repo_root,
        )

        if not isinstance(result, tuple) or len(result) != 2:
            ks_active = True
            try:
                rtype = type(result).__name__
                rlen = len(result) if hasattr(result, "__len__") else "N/A"
            except Exception:
                rtype = "unknown"
                rlen = "N/A"
            ks_reason = (
                f"fail-closed: invalid kill_switch output "
                f"(type={rtype}, len={rlen})"
            )
        else:
            active_val, reason_val = result
            if not isinstance(active_val, bool):
                ks_active = True
                ks_reason = (
                    f"fail-closed: kill_switch active flag not bool "
                    f"(got {type(active_val).__name__})"
                )
            elif active_val:
                ks_active = True
                ks_reason = (
                    reason_val if reason_val is not None
                    else "kill_switch active (no reason provided)"
                )
            else:
                ks_active = False
                ks_reason = None

    except Exception as e:
        ks_active = True
        ks_reason = (
            f"fail-closed: kill_switch raised "
            f"{type(e).__name__}: {e}"
        )

    cfg["kill_switch_active"] = ks_active
    cfg["kill_switch_reason"] = ks_reason
    return cfg


def _find_new_candles(klines: list, last_processed_ts) -> list:
    """
    فهرست indexهای کندل‌های جدید (ts > last_processed_ts)، به ترتیب صعودی.

    - اگر last_processed_ts None باشد، همه‌ی کندل‌ها بازگردانده می‌شوند.
    - اگر klines خالی باشد، [].
    """
    if not klines:
        return []

    if last_processed_ts is None:
        return list(range(len(klines)))

    return [
        i for i, c in enumerate(klines)
        if int(c[0]) > last_processed_ts
    ]


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

    # ---- 1. State ----
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

    # ---- 2. Specs ----
    print(f"📋 Fetching symbol specs...")
    try:
        specs = get_symbol_specs(symbol)
    except Exception as e:
        print(f"❌ symbol_specs failed: {e}")
        sys.exit(1)
    print(f"  contract_multiplier : {specs['contract_multiplier']}")
    print()

    # ---- 3. Klines ----
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

    # ---- 4. New candles ----
    new_indices = _find_new_candles(klines, last_processed)

    if not new_indices:
        print(f"⏭  No new closed candle. Last processed = {last_processed}")
        print(f"   Latest available    = {int(klines[-1][0])}")
        print()
        print("=" * 70)
        print("✅ PAPER RUN — No-op (no new candle)")
        print("=" * 70)
        sys.exit(0)

    total_new = len(new_indices)
    if total_new > MAX_CATCHUP_CANDLES:
        to_process = new_indices[:MAX_CATCHUP_CANDLES]
        lag = total_new - MAX_CATCHUP_CANDLES
    else:
        to_process = new_indices
        lag = 0

    first_ts = int(klines[to_process[0]][0])
    last_to_process_ts = int(klines[to_process[-1]][0])

    print(f"🆕 {total_new} new candles detected")
    print(f"   processing    : {len(to_process)}")
    print(f"   catchup_lag   : {lag}")
    print(f"   from_ts       : {first_ts}")
    print(f"   to_ts         : {last_to_process_ts}")
    print()

    # ---- 5. Indicators ----
    print("🧠 Computing indicators...")
    try:
        indicators = _build_indicators(klines)
    except Exception as e:
        print(f"❌ indicators failed: {e}")
        sys.exit(1)
    print(f"  ✅ computed")
    print()

    # ---- 6. Kill switch (once per run) ----
    engine_config = _build_engine_config_with_kill_switch(
        specs, state, state_dir, repo_root=os.getcwd(),
    )
    ks_active = engine_config.get("kill_switch_active")
    ks_reason = engine_config.get("kill_switch_reason")
    print(f"🛑 Kill switch check:")
    print(f"   active : {ks_active}")
    print(f"   reason : {ks_reason}")
    print()

    # ---- 7. Process each new candle in order ----
    print(f"⚙️  Processing {len(to_process)} candle(s)...")
    total_events_generated = 0

    for n, idx in enumerate(to_process, start=1):
        candle_ts = int(klines[idx][0])
        sig = evaluate_signal(indicators, idx)
        signal_label = sig.get("signal", "NO_TRADE")

        try:
            state, events = process_candle(
                state, klines, indicators, idx, specs, engine_config,
                signal=sig,
            )
        except Exception as e:
            print(f"  [{n}/{len(to_process)}] idx={idx} ts={candle_ts} "
                  f"signal={signal_label} → ERROR: "
                  f"{type(e).__name__}: {e}")
            print()
            print("❌ paper_engine failed mid-catchup")
            print("   Last successful state was saved before this failure.")
            sys.exit(1)

        # events را append کن
        if events:
            n_app, n_sk = append_events(state_dir, events)
            total_events_generated += n_app
        else:
            n_app, n_sk = 0, 0

        # state را به‌روز کن
        state["last_processed_candle_ts"] = candle_ts
        state["symbol"] = symbol
        state["contract_multiplier"] = specs["contract_multiplier"]
        state["last_close_price"] = float(klines[idx][4])
        state["catchup_lag_candles"] = lag

        try:
            save_state(state_dir, state)
        except Exception as e:
            print(f"  [{n}/{len(to_process)}] idx={idx} ts={candle_ts} "
                  f"signal={signal_label} → SAVE ERROR: {e}")
            sys.exit(1)

        print(f"  [{n}/{len(to_process)}] idx={idx} ts={candle_ts} "
              f"signal={signal_label} events={len(events)}")

    print()

    # ---- 8. Summary ----
    total_events_count = events_count(state_dir)

    print("=" * 70)
    print("📌 PAPER RUN SUMMARY")
    print("=" * 70)
    print(f"  candles_processed  : {len(to_process)}")
    print(f"  first_candle_ts    : {first_ts}")
    print(f"  last_candle_ts     : {last_to_process_ts}")
    print(f"  catchup_lag        : {lag}")
    print(f"  total_new_detected : {total_new}")
    print(f"  events_generated   : {total_events_generated}")
    print(f"  total_events       : {total_events_count}")
    print(f"  equity             : {state['equity']}")
    print(f"  peak_equity        : {state['peak_equity']}")
    print(f"  has_position       : {state['position'] is not None}")
    print(f"  daily_loss         : {state['daily_loss']}")
    print(f"  weekly_loss        : {state['weekly_loss']}")
    print(f"  circuit_breaker    : {state['circuit_breaker_active']}")
    if lag > 0:
        print()
        print(f"  ⚠️  catchup lag of {lag} candles remains")
        print(f"     (will be processed on next run)")
    print("=" * 70)
    print("✅ PAPER RUN COMPLETED")


if __name__ == "__main__":
    main()