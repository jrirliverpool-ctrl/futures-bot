"""
indicators.py — پیاده‌سازی Pure Python اندیکاتورها.

تأکید: هیچ کتابخانه خارجی استفاده نمی‌شود تا Determinism کامل حفظ شود.
"""


def ema(values: list, period: int) -> list:
    """
    محاسبه EMA (Exponential Moving Average).
    
    Returns: لیستی به اندازه ورودی، با None برای مقادیر ابتدایی
    (تا زمانی که داده کافی برای seed وجود نداشته باشد).
    
    Seed: SMA از period اول (استاندارد).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    # Seed با SMA
    sma_seed = sum(values[:period]) / period
    result[period - 1] = sma_seed

    # Multiplier
    alpha = 2.0 / (period + 1)

    # ادامه EMA
    prev = sma_seed
    for i in range(period, len(values)):
        current = values[i] * alpha + prev * (1 - alpha)
        result[i] = current
        prev = current

    return result


def true_range(high: list, low: list, close: list) -> list:
    """
    محاسبه True Range.
    
    TR[i] = max(
        high[i] - low[i],
        abs(high[i] - close[i-1]),
        abs(low[i] - close[i-1])
    )
    
    TR[0] = high[0] - low[0]  (بدون close قبلی)
    """
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high, low, close must have equal length")

    tr = [None] * len(high)
    if len(high) == 0:
        return tr

    tr[0] = high[0] - low[0]
    for i in range(1, len(high)):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    return tr


def atr(high: list, low: list, close: list, period: int = 14) -> list:
    """
    محاسبه ATR با روش Wilder (RMA).
    
    ATR[0..period-2] = None
    ATR[period-1] = SMA از TR[0..period-1]
    ATR[i] = (ATR[i-1] * (period-1) + TR[i]) / period
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high, low, close must have equal length")
    if len(high) < period:
        return [None] * len(high)

    tr = true_range(high, low, close)
    result = [None] * len(tr)

    # Seed: SMA از TR
    seed = sum(tr[:period]) / period
    result[period - 1] = seed

    # Wilder smoothing
    prev = seed
    for i in range(period, len(tr)):
        current = (prev * (period - 1) + tr[i]) / period
        result[i] = current
        prev = current

    return result


def donchian_high(high: list, period: int = 20) -> list:
    """
    بالاترین سقف در period کندل گذشته (شامل کندل جاری).
    
    Donchian_High[i] = max(high[i-period+1 : i+1])
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(high) < period:
        return [None] * len(high)

    result = [None] * len(high)
    for i in range(period - 1, len(high)):
        result[i] = max(high[i - period + 1 : i + 1])
    return result


def donchian_low(low: list, period: int = 20) -> list:
    """
    پایین‌ترین کف در period کندل گذشته (شامل کندل جاری).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(low) < period:
        return [None] * len(low)

    result = [None] * len(low)
    for i in range(period - 1, len(low)):
        result[i] = min(low[i - period + 1 : i + 1])
    return result


def compute_all(klines: list, ema_fast_p: int = 50, ema_slow_p: int = 200,
                donchian_p: int = 20, atr_p: int = 14) -> dict:
    """
    محاسبه همه اندیکاتورها روی لیست کندل.
    
    هر کندل: [openTime_ms, open, high, low, close, volume, ...]
    ورودی به‌صورت strings است (طبق Toobit)، در اینجا به float تبدیل می‌شود.
    """
    if not klines:
        raise ValueError("klines is empty")

    timestamps = [int(k[0]) for k in klines]
    opens  = [float(k[1]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    return {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "ema_fast": ema(closes, ema_fast_p),
        "ema_slow": ema(closes, ema_slow_p),
        "donchian_high": donchian_high(highs, donchian_p),
        "donchian_low": donchian_low(lows, donchian_p),
        "atr": atr(highs, lows, closes, atr_p),
    }