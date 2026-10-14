"""
indicators.py — پیاده‌سازی Pure Python اندیکاتورها.

تأکید: هیچ کتابخانه خارجی استفاده نمی‌شود تا Determinism کامل حفظ شود.

اعتبارسنجی: compute_all پیش از هر محاسبه، مقادیر هر کندل را بررسی
می‌کند. این لایه مستقل از data_feed است و نباید به این فرض وابسته
باشد که داده حتماً از data_feed.py عبور کرده است.
"""


# ============================================================
# Validation helpers
# ============================================================

def _validate_value(value, name: str, index: int):
    """
    اعتبارسنجی یک مقدار عددی پس از تبدیل به float.

    Raises:
        ValueError: اگر مقدار NaN، Inf، غیرعددی یا bool باشد.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name}[{index}] is bool, not numeric")
    if not isinstance(value, float):
        raise ValueError(
            f"{name}[{index}] is not float: {type(value).__name__}"
        )
    if value != value:
        raise ValueError(f"{name}[{index}] is NaN")
    if value == float("inf") or value == float("-inf"):
        raise ValueError(f"{name}[{index}] is infinite ({value})")


def _validate_klines(klines: list):
    """
    اعتبارسنجی کامل klines پیش از محاسبه اندیکاتور.

    Raises:
        ValueError: اگر ساختار، مقادیر، یا روابط OHLC نامعتبر باشند.
    """
    if not isinstance(klines, list):
        raise ValueError(
            f"klines must be a list, got {type(klines).__name__}"
        )
    if not klines:
        raise ValueError("klines is empty")

    for i, k in enumerate(klines):
        if not isinstance(k, (list, tuple)):
            raise ValueError(
                f"candle[{i}] must be list/tuple, got {type(k).__name__}"
            )
        if len(k) < 6:
            raise ValueError(
                f"candle[{i}] has only {len(k)} fields, need >= 6"
            )

        # Convert to float
        try:
            o = float(k[1])
            h = float(k[2])
            l = float(k[3])
            c = float(k[4])
            v = float(k[5])
        except (ValueError, TypeError):
            raise ValueError(
                f"candle[{i}] OHLCV contains non-numeric value"
            )

        # Finite checks (NaN/Inf)
        for field_name, val in (
            ("open", o), ("high", h), ("low", l),
            ("close", c), ("volume", v),
        ):
            if val != val:
                raise ValueError(f"candle[{i}] {field_name} is NaN")
            if val == float("inf") or val == float("-inf"):
                raise ValueError(
                    f"candle[{i}] {field_name} is infinite ({val})"
                )

        # Positive prices
        for field_name, val in (
            ("open", o), ("high", h), ("low", l), ("close", c),
        ):
            if val <= 0:
                raise ValueError(
                    f"candle[{i}] {field_name} must be > 0, got {val}"
                )

        # Volume
        if v < 0:
            raise ValueError(
                f"candle[{i}] volume must be >= 0, got {v}"
            )

        # OHLC relationships
        if l > h:
            raise ValueError(
                f"candle[{i}] low ({l}) > high ({h})"
            )
        if l > min(o, c):
            raise ValueError(
                f"candle[{i}] low ({l}) > min(open, close) ({min(o, c)})"
            )
        if h < max(o, c):
            raise ValueError(
                f"candle[{i}] high ({h}) < max(open, close) ({max(o, c)})"
            )


# ============================================================
# Indicators
# ============================================================

def ema(values: list, period: int) -> list:
    """
    محاسبه EMA.

    Returns: لیستی به اندازه ورودی، با None برای مقادیر ابتدایی
    (تا زمانی که داده کافی برای seed وجود نداشته باشد).

    Seed: SMA از period اول (استاندارد).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    sma_seed = sum(values[:period]) / period
    result[period - 1] = sma_seed

    alpha = 2.0 / (period + 1)

    prev = sma_seed
    for i in range(period, len(values)):
        current = values[i] * alpha + prev * (1 - alpha)
        result[i] = current
        prev = current

    return result


def true_range(high: list, low: list, close: list) -> list:
    """
    True Range.

    TR[i] = max(
        high[i] - low[i],
        abs(high[i] - close[i-1]),
        abs(low[i] - close[i-1])
    )
    TR[0] = high[0] - low[0]
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
    ATR با روش Wilder (RMA).

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

    seed = sum(tr[:period]) / period
    result[period - 1] = seed

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

    Raises:
        ValueError: اگر ساختار یا مقادیر هر کندل نامعتبر باشد
                    (شامل NaN / Inf / قیمت غیرمثبت / روابط OHLC نادرست).
    """
    # اعتبارسنجی مستقل — پیش از هر تبدیل
    _validate_klines(klines)

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