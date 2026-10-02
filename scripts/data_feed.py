"""
data_feed.py — لایه دریافت داده بازار از Toobit.

مسئولیت‌ها:
  - دریافت Klines از endpoint عمومی
  - Retry با exponential backoff
  - Retry روی timeout / connection / HTTP 429 / HTTP 5xx
  - عدم retry روی HTTP 4xx (به‌جز 429)
  - فیلتر کندل‌های بسته‌شده
  - اعتبارسنجی interval و limit
  - بدون دخالت در Strategy / Position / Order

Timestamp فیلد اول Kline = candle open time (ms).
"""

import time
import requests
from datetime import datetime, timezone


BASE_URL = "https://api.toobit.com"
KLINES_PATH = "/quote/v1/klines"

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


def _validate_fetch_args(interval: str, limit: int, max_retries: int):
    if interval not in INTERVAL_MS:
        raise ValueError(
            f"Unsupported interval: {interval}. "
            f"Supported: {SUPPORTED_INTERVALS}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be int in [1, 1000]")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be int in [1, 1000]")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool):
        raise ValueError("max_retries must be int >= 1")
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")


def _should_retry_http(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 300,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    timeout: int = 15,
    session=None,
) -> list:
    """
    دریافت Klines خام از Toobit.

    Retry:
      - timeout
      - connection errors
      - HTTP 429
      - HTTP 5xx

    No retry (propagate immediately):
      - HTTP 4xx (به‌جز 429)  → requests.HTTPError
      - invalid JSON / structure → ValueError
    """
    _validate_fetch_args(interval, limit, max_retries)
    if backoff_base < 0:
        raise ValueError("backoff_base must be >= 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    params = {"symbol": symbol, "interval": interval, "limit": limit}
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
            # Non-retryable 4xx → propagate HTTPError directly (no wrapping).
            response.raise_for_status()
            # Defensive fallback if raise_for_status didn't raise:
            raise requests.HTTPError(f"HTTP {status}")

        # Success path
        try:
            data = response.json()
        except ValueError:
            raise ValueError("Response body is not valid JSON")

        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected response type: {type(data).__name__}"
            )
        return data

    raise RuntimeError(
        f"Failed to fetch klines after {max_retries} attempts: {last_error}"
    )


def is_candle_closed(candle_ts_ms: int, now_ms: int, interval: str) -> bool:
    """
    بررسی بسته بودن کندل.
    close_time = open_time + interval
    بسته اگر close_time <= now
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    close_time = candle_ts_ms + INTERVAL_MS[interval]
    return close_time <= now_ms


def filter_closed_candles(candles: list, interval: str, now_ms: int = None) -> list:
    """فقط کندل‌های بسته‌شده. ترتیب اصلی حفظ می‌شود."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [
        candle for candle in candles
        if is_candle_closed(int(candle[0]), now_ms, interval)
    ]


def fetch_closed_klines(symbol: str, interval: str, limit: int = 300, **kwargs) -> list:
    """Fetch + closed-candle filtering."""
    raw = fetch_klines(symbol, interval, limit, **kwargs)
    return filter_closed_candles(raw, interval)


def candle_ts_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()