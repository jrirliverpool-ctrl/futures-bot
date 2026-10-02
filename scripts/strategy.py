"""
strategy.py — منطق استراتژی ApexQuant V1.

Pure Function. هیچ state، هیچ سفارش، هیچ SL/TP در این فایل نیست.
فقط اندیکاتور می‌گیرد و LONG/SHORT/NO_TRADE برمی‌گرداند.

Spec: docs/strategy_v1.md — Sections 5, 6

قرارداد با indicators.py:
    - ema_fast[index], ema_slow[index], atr[index]: شامل کندل سیگنال (index)
    - donchian_high[index-1], donchian_low[index-1]: فقط period کندل قبلی
      (خود کندل سیگنال در محاسبه Breakout دخیل نیست)

قرارداد با paper_engine.py:
    - signal_candle_ts: timestamp کندل بسته‌شده‌ای که سیگنال روی آن تولید شد
    - entry_reference_candle_index: index + 1
        ⚠️ این مقدار ممکن است برابر len(closes) باشد — یعنی کندل بعدی
        هنوز دریافت نشده. Paper Engine موظف است قبل از Entry بررسی کند
        که آیا کندل بعدی در دسترس هست یا نه.
    - atr_at_signal: ATR کندل سیگنال (برای محاسبه SL/TP توسط Paper Engine)
    - close: قیمت بسته شدن کندل سیگنال (فقط برای reference؛
        Entry واقعی با Open کندل بعد انجام می‌شود)

نکته: این تابع این موارد را در نظر نمی‌گیرد:
    - پوزیشن باز (مسئولیت Paper Engine)
    - Circuit Breaker (مسئولیت Paper Engine)
    - Cooldown (مسئولیت Paper Engine)
"""

STRATEGY_VERSION = "1.0.0"


def evaluate_signal(indicators: dict, index: int) -> dict:
    """
    ارزیابی سیگنال در کندل با اندیس index.

    ورودی:
        indicators: dict خروجی compute_all() از indicators.py
        index: اندیس کندل بسته‌شده‌ای که می‌خواهیم سیگنال را روی آن ارزیابی کنیم

    خروجی (dict):
        signal: "LONG" | "SHORT" | "NO_TRADE"
        reason: توضیح انسانی (برای لاگ)
        signal_candle_ts: timestamp کندل سیگنال (ms)
        close, ema_fast, ema_slow, donchian_ref, atr_at_signal
        entry_reference_candle_index: index + 1 (ممکن است برابر len(closes) باشد)
        strategy_version: نسخه استراتژی
    """
    # --- Validation ---
    if not isinstance(indicators, dict):
        return _no_trade("indicators must be a dict")

    required_keys = ["timestamps", "closes", "ema_fast", "ema_slow",
                     "donchian_high", "donchian_low", "atr"]
    for k in required_keys:
        if k not in indicators:
            return _no_trade(f"missing indicator key: {k}")

    n = len(indicators["closes"])
    if index < 0 or index >= n:
        return _no_trade(f"index out of range: {index} (len={n})")

    # Donchian از index-1 استفاده می‌کند چون باید سقف/کف کندل‌های قبلی باشد،
    # نه شامل خود کندل سیگنال.
    if index < 1:
        return _no_trade("not enough candles for donchian reference")

    ema_fast_val = indicators["ema_fast"][index]
    ema_slow_val = indicators["ema_slow"][index]
    atr_val = indicators["atr"][index]
    dh_prev = indicators["donchian_high"][index - 1]
    dl_prev = indicators["donchian_low"][index - 1]

    if (ema_fast_val is None or ema_slow_val is None or
            atr_val is None or dh_prev is None or dl_prev is None):
        return _no_trade("warmup incomplete")

    close = indicators["closes"][index]
    ts = indicators["timestamps"][index]

    # --- Evaluate conditions ---
    trend_up = close > ema_slow_val and ema_fast_val > ema_slow_val
    trend_down = close < ema_slow_val and ema_fast_val < ema_slow_val

    breakout_up = close > dh_prev
    breakdown_down = close < dl_prev

    long_conditions = trend_up and breakout_up
    short_conditions = trend_down and breakdown_down

    # Defensive: Long و Short نباید همزمان فعال شوند
    if long_conditions and short_conditions:
        return _no_trade(
            "ambiguous: both long and short conditions met",
            close=close, ema_fast=ema_fast_val, ema_slow=ema_slow_val,
            atr=atr_val, ts=ts,
        )

    if long_conditions:
        return _signal(
            "LONG",
            "close>ema200 & ema50>ema200 & close>donchian_high[prev]",
            close=close, ema_fast=ema_fast_val, ema_slow=ema_slow_val,
            donchian_ref=dh_prev, atr=atr_val, ts=ts,
            entry_index=index + 1,
        )

    if short_conditions:
        return _signal(
            "SHORT",
            "close<ema200 & ema50<ema200 & close<donchian_low[prev]",
            close=close, ema_fast=ema_fast_val, ema_slow=ema_slow_val,
            donchian_ref=dl_prev, atr=atr_val, ts=ts,
            entry_index=index + 1,
        )

    # --- NO_TRADE: ساخت reason گویا ---
    reasons = []
    if not trend_up and not trend_down:
        reasons.append("no clear trend (close/ema relationship)")
    elif trend_up and not breakout_up:
        reasons.append(
            f"trend up but close({close}) <= donchian_high[prev]({dh_prev})"
        )
    elif trend_down and not breakdown_down:
        reasons.append(
            f"trend down but close({close}) >= donchian_low[prev]({dl_prev})"
        )

    return _no_trade(
        " ; ".join(reasons) or "no signal",
        close=close, ema_fast=ema_fast_val, ema_slow=ema_slow_val,
        atr=atr_val, ts=ts,
    )


def _signal(signal_type, reason, close, ema_fast, ema_slow,
            donchian_ref, atr, ts, entry_index):
    return {
        "signal": signal_type,
        "reason": reason,
        "signal_candle_ts": ts,
        "close": close,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "donchian_ref": donchian_ref,
        "atr_at_signal": atr,
        "entry_reference_candle_index": entry_index,
        "strategy_version": STRATEGY_VERSION,
    }


def _no_trade(reason, close=None, ema_fast=None, ema_slow=None,
              atr=None, ts=None):
    return {
        "signal": "NO_TRADE",
        "reason": reason,
        "signal_candle_ts": ts,
        "close": close,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "donchian_ref": None,
        "atr_at_signal": atr,
        "entry_reference_candle_index": None,
        "strategy_version": STRATEGY_VERSION,
    }