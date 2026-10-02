"""
test_indicators.py — تست‌های واحد برای اندیکاتورها.

مرجع مقایسه: مقادیر دستی محاسبه‌شده از فرمول استاندارد EMA/ATR/Donchian.
این تست‌ها وابستگی خارجی ندارند و Deterministic هستند.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from indicators import ema, atr, true_range, donchian_high, donchian_low


def test_ema_simple():
    """EMA(3) روی [1,2,3,4,5,6] — بررسی دستی."""
    values = [1, 2, 3, 4, 5, 6]
    result = ema(values, 3)

    assert result[0] is None
    assert result[1] is None
    assert abs(result[2] - 2.0) < 1e-9
    assert abs(result[3] - 3.0) < 1e-9
    assert abs(result[4] - 4.0) < 1e-9
    assert abs(result[5] - 5.0) < 1e-9
    print("✅ test_ema_simple passed")


def test_ema_too_short():
    """اگر داده کمتر از period باشد، همه None."""
    result = ema([1, 2], 5)
    assert result == [None, None]
    print("✅ test_ema_too_short passed")


def test_true_range_basic():
    """TR روی کندل ساده."""
    high  = [10, 12, 11]
    low   = [8,  9,  10]
    close = [9,  11, 10]

    tr = true_range(high, low, close)

    assert abs(tr[0] - 2.0) < 1e-9
    assert abs(tr[1] - 3.0) < 1e-9
    assert abs(tr[2] - 1.0) < 1e-9
    print("✅ test_true_range_basic passed")


def test_atr_simple():
    """
    ATR(3) روی داده کوچک — تأیید Wilder smoothing.
    
    TR = [2, 3, 1, 3, 2]
    Seed: ATR[2] = SMA(TR[0..2]) = (2+3+1)/3 = 2.0
    ATR[3] = (ATR[2]×2 + TR[3]) / 3 = (2×2 + 3)/3 = 7/3 ≈ 2.333333
    ATR[4] = (ATR[3]×2 + TR[4]) / 3 = (2.3333×2 + 2)/3 ≈ 2.222222
    """
    high  = [10, 12, 11, 13, 14]
    low   = [8,  9,  10, 11, 12]
    close = [9,  11, 10, 12, 13]

    result = atr(high, low, close, 3)

    assert abs(result[2] - 2.0) < 1e-9
    assert abs(result[3] - (7.0/3.0)) < 1e-9
    assert abs(result[4] - ((7.0/3.0)*2 + 2.0)/3.0) < 1e-9
    print(f"✅ test_atr_simple passed (ATR[3]={result[3]:.6f}, ATR[4]={result[4]:.6f})")


def test_donchian():
    """Donchian(3) روی داده ساده."""
    high = [5, 7, 6, 9, 8, 10]
    low  = [3, 4, 2, 5, 4, 6]

    dh = donchian_high(high, 3)
    dl = donchian_low(low, 3)

    assert dh[2] == 7
    assert dh[3] == 9
    assert dh[4] == 9
    assert dh[5] == 10

    assert dl[2] == 2
    assert dl[3] == 2
    assert dl[4] == 2
    assert dl[5] == 4
    print("✅ test_donchian passed")


def test_ema_reference_against_known():
    """
    مقایسه EMA(10) با مقادیر مرجع دستی.
    داده ورودی: 1..20
    Seed: SMA(1..10) = 5.5
    EMA[10] = 11 × (2/11) + 5.5 × (9/11) = 6.5
    """
    values = list(range(1, 21))
    result = ema(values, 10)

    assert abs(result[9] - 5.5) < 1e-9

    alpha = 2.0 / 11.0
    expected_10 = 11 * alpha + 5.5 * (1 - alpha)
    assert abs(result[10] - expected_10) < 1e-9

    print(f"✅ test_ema_reference_against_known passed (EMA[10]={result[10]:.6f})")


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Running indicator unit tests")
    print("=" * 60)

    tests = [
        test_ema_simple,
        test_ema_too_short,
        test_true_range_basic,
        test_atr_simple,
        test_donchian,
        test_ema_reference_against_known,
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