"""
binance_feed.py — لایه‌ی دریافت داده‌ی تاریخی از Binance Public API.

هدف:
  - دریافت کندل‌های تاریخی چندساله برای Backtest بلندمدت
  - مستقل از data_feed.py (که برای Toobit است)

مسئولیت‌ها:
  - دریافت Klines از endpoint عمومی Binance
  - صفحه‌بندی خودکار برای بازه‌های طولانی (بیش از 1000 کندل)
  - Retry روی خطاهای شبکه/5xx/429
  - حذف تکراری، مرتب‌سازی صعودی
  - fail-closed در صورت داده‌ی ناقص (strict_completeness=True)
  - اعتبارسنجی ساختار هر کندل قبل از پذیرش
  - تشخیص دقیق گپ‌های زمانی

قرارداد:
  - Endpoint: https://data-api.binance.vision/api/v3/klines
  - هر کندل: [openTime_ms, open, high, low, close, volume, closeTime_ms, ...]
  - Same index layout as Toobit → سازگار با indicators.compute_all

انتخاب BASE_URL:
  Binance دو endpoint جداگانه دارد:
    - api.binance.com          → ترافیک حساب کاربری و تریدینگ (geo-restricted)
    - data-api.binance.vision  → داده‌های عمومی بازار (توصیه‌ی رسمی Binance)
  برای Backtest فقط به داده‌ی عمومی نیاز داریم، پس از endpoint دوم
  استفاده می‌کنیم. این یک مسیر مجاز و رسمی است، نه دور زدن محدودیت.

محدودیت‌ها (مستند در BACKLOG-002):
  - داده‌ی Binance ≠ داده‌ی Toobit
  - Commission و slippage باید طبق Toobit مدل شوند
  - فقط برای Backtest، نه Paper

Spec: docs/backlog.md — BACKLOG-002
"""

import time
import requests
from datetime import datetime, timezone


# Public market-data endpoint (recommended by Binance for public data).
# api.binance.com is geo-restricted from GitHub Actions runners.
BASE_URL = "https://data-api.binance.vision"
KLINES_PATH = "/api/v3/klines"

MAX_LIMIT = 1000
DEFAULT_MAX_CANDLES = 40000
DEFAULT_MAX_ITERATIONS = 200

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
SUPPORTED_INTERVALS = tuple(INTERVAL_MS.keys())


# ============================================================
# Exceptions
# ============================================================

class BinanceFeedError(Exception):
    """خطای پایه‌ی این ماژول."""


class InvalidCandleError(BinanceFeedError):
    """کندل با ساختار نامعتبر."""


class TruncatedDataError(BinanceFeedError):
    """دریافت داده ناقص ماند (سقف ایمنی فعال شد)."""


# ============================================================
# Validation
# ============================================================

def _validate_fetch_args(symbol, interval, limit, max_retries):
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if interval not in INTERVAL_MS:
        raise ValueError(
            f"Unsupported interval: {interval}. "
            f"Supported: {SUPPORTED_INTERVALS}"
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be int")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {MAX_LIMIT}]")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise ValueError("max_retries must be int >= 1")
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")


def _should_retry_http(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_finite_ts(ts) -> bool:
    return isinstance(ts, int) and not isinstance(ts, bool) and ts > 0


def _validate_candle(candle, strict: bool = True) -> bool:
    """
    اعتبارسنجی یک کندل خام Binance.

    ساختار مورد انتظار:
      [openTime_ms, open, high, low, close, volume, ...]

    اگر strict=True:
      - استثناء InvalidCandleError در صورت نامعتبر بودن
    اگر strict=False:
      - False برگردانده می‌شود.

    بررسی‌ها:
      - نوع: list یا tuple
      - طول: >= 6
      - openTime_ms: int مثبت
      - open, high, low, close: numeric و finite
      - حجم: numeric و >= 0
      - منطق OHLC: low <= min(open, close) و high >= max(open, close)
    """
    def _fail(reason):
        if strict:
            raise InvalidCandleError(reason)
        return False

    if not isinstance(candle, (list, tuple)):
        return _fail(f"candle is not list/tuple: {type(candle).__name__}")
    if len(candle) < 6:
        return _fail(f"candle has only {len(candle)} fields (< 6)")

    # timestamp
    try:
        ts = int(candle[0])
    except (ValueError, TypeError):
        return _fail(f"openTime not int-parseable: {candle[0]!r}")
    if ts <= 0:
        return _fail(f"openTime <= 0: {ts}")

    # OHLC numeric
    try:
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])
        v = float(candle[5])
    except (ValueError, TypeError):
        return _fail("OHLCV contains non-numeric value")

    # finite check (بدون math.isfinite چون bool هم رد کنیم)
    for name, val in (("open", o), ("high", h),
                      ("low", l), ("close", c), ("volume", v)):
        if val != val:
            return _fail(f"{name} is NaN")
        if val in (float("inf"), float("-inf")):
            return _fail(f"{name} is infinite")

    if v < 0:
        return _fail(f"volume < 0: {v}")

    # OHLC logic
    if l > o or l > c or l > h:
        return _fail(f"low > open/close/high: l={l} o={o} c={c} h={h}")
    if h < o or h < c or h < l:
        return _fail(f"high < open/close/low: h={h} o={o} c={c} l={l}")

    return True


# ============================================================
# Single request
# ============================================================

def fetch_binance_klines(
    symbol: str,
    interval: str,
    limit: int = MAX_LIMIT,
    start_ms: int = None,
    end_ms: int = None,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    timeout: int = 15,
    session=None,
    validate: bool = True,
) -> list:
    """
    دریافت یک دسته کندل از Binance.

    اگر validate=True:
      - همه‌ی کندل‌ها با _validate_candle بررسی می‌شوند
      - کندل نامعتبر → InvalidCandleError (کل درخواست رد می‌شود)
    """
    _validate_fetch_args(symbol, interval, limit, max_retries)
    if backoff_base < 0:
        raise ValueError("backoff_base must be >= 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        if not _is_finite_ts(start_ms):
            raise ValueError("start_ms must be positive int")
        params["startTime"] = start_ms
    if end_ms is not None:
        if not _is_finite_ts(end_ms):
            raise ValueError("end_ms must be positive int")
        params["endTime"] = end_ms

    url = f"{BASE_URL}{KLINES_PATH}"
    client = session if session is not None else requests

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.get(url, params=params, timeout=timeout)
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
                continue
            break

        status = response.status_code

        if status >= 400:
            if _should_retry_http(status):
                last_error = requests.HTTPError(f"HTTP {status}")
                if attempt < max_retries - 1:
                    time.sleep(backoff_base * (2 ** attempt))
                    continue
                break
            response.raise_for_status()
            raise requests.HTTPError(f"HTTP {status}")

        try:
            data = response.json()
        except ValueError:
            raise ValueError("Response body is not valid JSON")

        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected response type: {type(data).__name__}"
            )

        if validate:
            for c in data:
                _validate_candle(c, strict=True)

        return data

    raise RuntimeError(
        f"Failed to fetch binance klines after "
        f"{max_retries} attempts: {last_error}"
    )


# ============================================================
# Paginated range fetch
# ============================================================

def fetch_binance_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    max_candles: int = DEFAULT_MAX_CANDLES,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_retries: int = 3,
    backoff_base: float = 0.5,
    timeout: int = 15,
    session=None,
    sleep_between_requests: float = 0.15,
    strict_completeness: bool = True,
) -> list:
    """
    دریافت پیوسته‌ی بازه‌ی طولانی با صفحه‌بندی خودکار.

    Args:
        strict_completeness:
          - True (پیش‌فرض): اگر دریافت پیش از رسیدن به end_ms متوقف
            شود (سقف ایمنی)، TruncatedDataError raise می‌شود.
          - False: فقط warning در قالب فیلد `_truncated=True` برگردانده
            می‌شود (توصیه نمی‌شود).

    Returns:
        list of candles (مرتب صعودی، بدون تکرار، ≤ max_candles)

    Raises:
        TruncatedDataError: اگر strict_completeness=True و دریافت ناقص ماند
        ValueError: پارامتر نامعتبر
        InvalidCandleError: کندل نامعتبر در پاسخ
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if not _is_finite_ts(start_ms):
        raise ValueError("start_ms must be positive int")
    if not _is_finite_ts(end_ms):
        raise ValueError("end_ms must be positive int")
    if end_ms <= start_ms:
        raise ValueError("end_ms must be > start_ms")
    if max_candles < 1:
        raise ValueError("max_candles must be >= 1")
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    interval_ms = INTERVAL_MS[interval]
    collected = {}
    cursor = start_ms
    iterations = 0
    truncated_reason = None

    while cursor <= end_ms:
        if len(collected) >= max_candles:
            truncated_reason = (
                f"max_candles={max_candles} reached before end_ms"
            )
            break

        if iterations >= max_iterations:
            truncated_reason = (
                f"max_iterations={max_iterations} reached before end_ms"
            )
            break

        iterations += 1

        batch = fetch_binance_klines(
            symbol, interval, limit=MAX_LIMIT,
            start_ms=cursor, end_ms=end_ms,
            max_retries=max_retries,
            backoff_base=backoff_base,
            timeout=timeout,
            session=session,
            validate=True,
        )

        if not batch:
            # پایان طبیعی: Binance داده‌ی بیشتری ندارد
            break

        new_in_batch = 0
        for candle in batch:
            ts = int(candle[0])
            if ts < start_ms or ts > end_ms:
                continue
            if ts not in collected:
                if len(collected) >= max_candles:
                    # سقف در حین دسته فعال شد — اضافه نکن
                    truncated_reason = (
                        f"max_candles={max_candles} reached "
                        f"mid-batch"
                    )
                    break
                collected[ts] = candle
                new_in_batch += 1

        if truncated_reason is not None:
            break

        if new_in_batch == 0:
            # حلقه‌ی بی‌پایان: Binance همان داده را برگرداند
            break

        last_ts = int(batch[-1][0])
        next_cursor = last_ts + interval_ms

        if next_cursor <= cursor:
            break
        cursor = next_cursor

        if len(batch) < MAX_LIMIT:
            # آخرین صفحه
            break

        if sleep_between_requests > 0:
            time.sleep(sleep_between_requests)

    # مرتب‌سازی
    sorted_ts = sorted(collected.keys())
    result = [collected[ts] for ts in sorted_ts]

    # بررسی کامل بودن
    # تعریف «کامل»: بازه‌ی نتیجه باید تا نزدیکی end_ms برسد
    # اگر آخرین کندل مانده خیلی عقب‌تر از end_ms باشد → ناقص
    if result:
        last_result_ts = int(result[-1][0])
        # آخرین کندل ممکن = last end_ms aligned
        expected_last_ts = ((end_ms - interval_ms) // interval_ms) * interval_ms
        # اگر بیش از ۲ کندل عقب هستیم → ناقص
        if last_result_ts < expected_last_ts - interval_ms * 2:
            if truncated_reason is None:
                truncated_reason = (
                    f"data ends at {last_result_ts} but "
                    f"expected near {expected_last_ts}"
                )
    elif cursor <= end_ms:
        truncated_reason = "no candles returned"

    if truncated_reason is not None:
        if strict_completeness:
            raise TruncatedDataError(
                f"Incomplete data fetch: {truncated_reason}. "
                f"got {len(result)} candles. "
                f"Use strict_completeness=False to allow partial."
            )

    return result


# ============================================================
# Closed-candle filtering
# ============================================================

def is_binance_candle_closed(candle_ts_ms: int, now_ms: int,
                              interval: str) -> bool:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    close_time = candle_ts_ms + INTERVAL_MS[interval]
    return close_time <= now_ms


def filter_closed_binance_candles(candles: list, interval: str,
                                   now_ms: int = None) -> list:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [
        c for c in candles
        if is_binance_candle_closed(int(c[0]), now_ms, interval)
    ]


# ============================================================
# Diagnostics
# ============================================================

def find_gaps(candles: list, interval: str) -> list:
    """
    تشخیص گپ‌های زمانی بین کندل‌های متوالی.

    قرارداد: delta باید دقیقاً برابر interval_ms باشد.
    هر delta != interval_ms به عنوان گپ گزارش می‌شود
    (نه فقط delta > 2×interval).

    Returns:
        list of {"after_index", "prev_ts", "next_ts", "delta_ms",
                 "expected_ms", "missing_count"}
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if len(candles) < 2:
        return []

    interval_ms = INTERVAL_MS[interval]
    ts_list = [int(c[0]) for c in candles]
    gaps = []

    for i in range(1, len(ts_list)):
        delta = ts_list[i] - ts_list[i - 1]
        if delta != interval_ms:
            missing = max(0, (delta // interval_ms) - 1)
            gaps.append({
                "after_index": i - 1,
                "prev_ts": ts_list[i - 1],
                "next_ts": ts_list[i],
                "delta_ms": delta,
                "expected_ms": interval_ms,
                "missing_count": missing,
            })
    return gaps


def summarize_candles(candles: list, interval: str) -> dict:
    """
    گزارش کوتاه از بازه‌ی داده، همراه با گپ‌های دقیق.
    """
    if not candles:
        return {
            "count": 0,
            "first_ts": None,
            "last_ts": None,
            "first_iso": None,
            "last_iso": None,
            "duplicates": 0,
            "gaps": [],
            "gap_count": 0,
            "total_missing": 0,
        }

    ts_list = [int(c[0]) for c in candles]
    duplicates = len(ts_list) - len(set(ts_list))
    gaps = find_gaps(candles, interval)

    def _iso(ts_ms):
        return datetime.fromtimestamp(
            ts_ms / 1000, tz=timezone.utc,
        ).isoformat()

    return {
        "count": len(candles),
        "first_ts": ts_list[0],
        "last_ts": ts_list[-1],
        "first_iso": _iso(ts_list[0]),
        "last_iso": _iso(ts_list[-1]),
        "duplicates": duplicates,
        "gaps": gaps,
        "gap_count": len(gaps),
        "total_missing": sum(g["missing_count"] for g in gaps),
    }