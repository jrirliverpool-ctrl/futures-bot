"""
test_strategy.py — تست‌های واحد برای منطق استراتژی ApexQuant V1.

تست‌ها با اندیکاتورهای ساختگی (mock) انجام می‌شوند تا فقط منطق تصمیم‌گیری
مورد ارزیابی قرار گیرد، نه صحت محاسبات اندیکاتور (که در test_indicators.py
جداگانه تست شده است).
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from strategy import evaluate_signal, STRATEGY_VERSION


def _make_inds(n, idx, close, ema_fast, ema_slow, dh, dl, atr):
    """ساخت یک indicators dict مصنوعی برای تست."""
    timestamps = list(range(n))
    closes = [0.0] * n
    closes[idx] = close

    ema_fast_arr = [None] * n
    ema_slow_arr = [None] * n
    atr_arr = [None] * n
    dh_arr = [None] * n
    dl_arr = [None] * n

    ema_fast_arr[idx] = ema_fast
    ema_slow_arr[idx] = ema_slow
    atr_arr[idx] = atr
    dh_arr[idx - 1] = dh
    dl_arr[idx - 1] = dl

    return {
        "timestamps": timestamps,
        "closes": closes,
        "ema_fast": ema_fast_arr,
        "ema_slow": ema_slow_arr,
        "donchian_high": dh_arr,
        "donchian_low": dl_arr,
        "atr": atr_arr,
    }


def test_warmup_incomplete():
    """اگر اندیکاتورها None باشند → NO_TRADE با دلیل warmup."""
    inds = _make_inds(n=11, idx=10, close=100, ema_fast=95, ema_slow=90,
                      dh=98, dl=80, atr=2.0)
    # خالی کردن ema_slow
    inds["ema_slow"][10] = None
    sig = evaluate_signal(inds, 10)
    assert sig["signal"] == "NO_TRADE"
    assert "warmup" in sig["reason"]
    print("✅ test_warmup_incomplete passed")


def test_long_signal():
    """شرایط کامل Long → LONG."""
    inds = _make_inds(n=11, idx=10, close=100, ema_fast=95, ema_slow=90,
                      dh=98, dl=80, atr=2.0)
    sig = evaluate_signal(inds, 10)
    assert sig["signal"] == "LONG"
    assert sig["entry_reference_candle_index"] == 11
    assert sig["strategy_version"] == STRATEGY_VERSION
    assert sig["atr_at_signal"] == 2.0
    print("✅ test_long_signal passed")


def test_short_signal():
    """شرایط کامل Short → SHORT."""
    inds = _make_inds(n=11, idx=10, close=80, ema_fast=85, ema_slow=90,
                      dh=100, dl=82, atr=2.0)
    sig = evaluate_signal(inds, 10)
    assert sig["signal"] == "SHORT"
    assert sig["entry_reference_candle_index"] == 11
    print("✅ test_short_signal passed")


def test_uptrend_no_breakout():
    """روند صعودی ولی Breakout رخ نداده → NO_TRADE."""
    inds = _make_inds(n=11, idx=10, close=97, ema_fast=95, ema_slow=90,
                      dh=98, dl=80, atr=2.0)
    sig = evaluate_signal(inds, 10)
    assert sig["signal"] == "NO_TRADE"
    assert "donchian_high" in sig["reason"]
    print("✅ test_uptrend_no_breakout passed")


def test_downtrend_no_breakdown():
    """روند نزولی ولی Breakdown رخ نداده → NO_TRADE."""
    inds = _make_inds(n=11, idx=10, close=83, ema_fast=85, ema_slow=90,
                      dh=100, dl=82, atr=2.0)
    sig = evaluate_signal(inds, 10)
    assert sig["signal"] == "NO_TRADE"
    assert "donchian_low" in sig["reason"]
    print("✅ test_downtrend_no_breakdown passed")


def test_weak_trend_no_signal():
    """EMA50 < EMA200 در حالی که close > EMA200 → NO_TRADE (روند ضعیف)."""
    inds = _make_inds(n=11, idx=10, close=100, ema_fast=93, ema_slow=95,
                      dh=98, dl=80, atr=2.0)
    sig = evaluate_signal(inds, 10)
    assert sig["signal"] == "NO_TRADE"
    assert "no clear trend" in sig["reason"]
    print("✅ test_weak_trend_no_signal passed")


def test_index_out_of_range():
    """index خارج از محدوده → NO_TRADE."""
    inds = _make_inds(n=11, idx=10, close=100, ema_fast=95, ema_slow=90,
                      dh=98, dl=80, atr=2.0)
    sig = evaluate_signal(inds, 99)
    assert sig["signal"] == "NO_TRADE"
    assert "out of range" in sig["reason"]
    print("✅ test_index_out_of_range passed")


def test_index_zero_no_donchian_ref():
    """index=0 → NO_TRADE چون Donchian reference وجود ندارد."""
    inds = _make_inds(n=11, idx=10, close=100, ema_fast=95, ema_slow=90,
                      dh=98, dl=80, atr=2.0)
    sig = evaluate_signal(inds, 0)
    assert sig["signal"] == "NO_TRADE"
    assert "donchian reference" in sig["reason"]
    print("✅ test_index_zero_no_donchian_ref passed")


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Running strategy unit tests")
    print("=" * 60)

    tests = [
        test_warmup_incomplete,
        test_long_signal,
        test_short_signal,
        test_uptrend_no_breakout,
        test_downtrend_no_breakdown,
        test_weak_trend_no_signal,
        test_index_out_of_range,
        test_index_zero_no_donchian_ref,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__} FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {t.__name__} ERROR: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)