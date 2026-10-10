"""
test_binance_feed.py — Unit tests for binance_feed.py.

تمام تست‌ها با MockSession اجرا می‌شوند.
هیچ درخواست واقعی به Binance ارسال نمی‌شود.

پوشش (38 تست):
  - Argument validation (5)
  - Retry policy (1)
  - Candle validation (7)
  - fetch_binance_klines (9)
  - fetch_binance_klines_range (8)
  - Closed candle (2)
  - Gap detection (3)
  - Summarize (3)

سیاست ترتیب:
  fetch_binance_klines_range، پاسخ نامرتب API را نرمال‌سازی می‌کند
  (حذف تکراری + مرتب‌سازی صعودی). این رفتار در تست
  test_range_dedup_and_sort پوشش داده شده.

سیاست گپ:
  fetch_binance_klines_range گپ‌های درونی را «قبول» می‌کند
  (fail نمی‌دهد). تشخیص گپ مسئولیت find_gaps است.
  تست test_range_multi_page_with_gap این قرارداد را تثبیت می‌کند.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from binance_feed import (
    fetch_binance_klines,
    fetch_binance_klines_range,
    is_binance_candle_closed,
    filter_closed_binance_candles,
    find_gaps,
    summarize_candles,
    _validate_candle,
    _validate_fetch_args,
    _should_retry_http,
    InvalidCandleError,
    TruncatedDataError,
    MAX_LIMIT,
    INTERVAL_MS,
)


# ============================================================
# Mock infrastructure
# ============================================================

class MockResponse:
    def __init__(self, status, json_data=None,
                 json_exc=None, raise_for_status_exc=None):
        self.status_code = status
        self._json = json_data
        self._json_exc = json_exc
        self._raise_exc = raise_for_status_exc
        self.text = "mock"

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc


class MockSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._responses:
            raise RuntimeError("MockSession has no responses left")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ============================================================
# Helpers
# ============================================================

def _candle(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=1000.0):
    return [
        ts,
        str(o), str(h), str(l), str(c), str(v),
        ts + 3_600_000 - 1,
        "0", 0, "0", "0", "0",
    ]


def _batch(start_ts, n, interval_ms=3_600_000):
    return [_candle(start_ts + i * interval_ms) for i in range(n)]


# ============================================================
# 1. Argument validation (5)
# ============================================================

def test_validate_args_ok():
    _validate_fetch_args("BTCUSDT", "1h", 1000, 3)
    print("✅ test_validate_args_ok")


def test_validate_args_bad_symbol():
    try:
        _validate_fetch_args("", "1h", 100, 3)
        assert False
    except ValueError:
        pass
    print("✅ test_validate_args_bad_symbol")


def test_validate_args_bad_interval():
    try:
        _validate_fetch_args("BTCUSDT", "2h", 100, 3)
        assert False
    except ValueError:
        pass
    print("✅ test_validate_args_bad_interval")


def test_validate_args_bad_limit():
    for bad in (0, 2000, -1, True, "100"):
        try:
            _validate_fetch_args("BTCUSDT", "1h", bad, 3)
            assert False
        except ValueError:
            pass
    print("✅ test_validate_args_bad_limit")


def test_validate_args_bad_retries():
    try:
        _validate_fetch_args("BTCUSDT", "1h", 100, 0)
        assert False
    except ValueError:
        pass
    print("✅ test_validate_args_bad_retries")


# ============================================================
# 2. Retry policy (1)
# ============================================================

def test_should_retry_http():
    assert _should_retry_http(429) is True
    assert _should_retry_http(500) is True
    assert _should_retry_http(502) is True
    assert _should_retry_http(599) is True
    assert _should_retry_http(400) is False
    assert _should_retry_http(404) is False
    assert _should_retry_http(200) is False
    print("✅ test_should_retry_http")


# ============================================================
# 3. Candle validation (7)
# ============================================================

def test_validate_candle_ok():
    assert _validate_candle(_candle(1_700_000_000_000)) is True
    print("✅ test_validate_candle_ok")


def test_validate_candle_not_list():
    try:
        _validate_candle("not a list")
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_validate_candle_not_list")


def test_validate_candle_short():
    try:
        _validate_candle([1000, "1", "2"])
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_validate_candle_short")


def test_validate_candle_bad_ts():
    try:
        _validate_candle(["not-int", "1", "2", "0", "1", "1"])
        assert False
    except InvalidCandleError:
        pass
    try:
        _validate_candle([-5, "1", "2", "0", "1", "1"])
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_validate_candle_bad_ts")


def test_validate_candle_non_numeric():
    try:
        _validate_candle([1000, "abc", "2", "0", "1", "1"])
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_validate_candle_non_numeric")


def test_validate_candle_negative_volume():
    try:
        _validate_candle([1000, "100", "101", "99", "100.5", "-1"])
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_validate_candle_negative_volume")


def test_validate_candle_bad_ohlc():
    try:
        _validate_candle([1000, "100", "99", "101", "100.5", "1"])
        assert False
    except InvalidCandleError:
        pass
    try:
        _validate_candle([1000, "100", "99.5", "99", "100.5", "1"])
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_validate_candle_bad_ohlc")


# ============================================================
# 4. fetch_binance_klines (9)
# ============================================================

def test_fetch_single_success():
    ts = 1_700_000_000_000
    batch = _batch(ts, 3)
    session = MockSession([MockResponse(200, json_data=batch)])
    result = fetch_binance_klines(
        "BTCUSDT", "1h", limit=10,
        session=session, backoff_base=0,
    )
    assert result == batch
    assert len(session.calls) == 1
    print("✅ test_fetch_single_success")


def test_fetch_invalid_response():
    session = MockSession([MockResponse(200, json_data={"not": "list"})])
    try:
        fetch_binance_klines("BTCUSDT", "1h",
                             session=session, backoff_base=0)
        assert False
    except ValueError as e:
        assert "Unexpected response type" in str(e)
    print("✅ test_fetch_invalid_response")


def test_fetch_invalid_json():
    session = MockSession([
        MockResponse(200, json_exc=ValueError("bad json")),
    ])
    try:
        fetch_binance_klines("BTCUSDT", "1h",
                             session=session, backoff_base=0)
        assert False
    except ValueError as e:
        assert "not valid JSON" in str(e)
    print("✅ test_fetch_invalid_json")


def test_fetch_invalid_candle_rejected():
    bad_batch = [[1000, "100", "101", "99", "100.5", "-1"]]
    session = MockSession([MockResponse(200, json_data=bad_batch)])
    try:
        fetch_binance_klines("BTCUSDT", "1h",
                             session=session, backoff_base=0)
        assert False
    except InvalidCandleError:
        pass
    print("✅ test_fetch_invalid_candle_rejected")


def test_fetch_retry_on_timeout():
    import requests
    ts = 1_700_000_000_000
    batch = _batch(ts, 3)
    session = MockSession([
        requests.exceptions.Timeout("t1"),
        MockResponse(200, json_data=batch),
    ])
    result = fetch_binance_klines(
        "BTCUSDT", "1h", session=session,
        backoff_base=0, max_retries=3,
    )
    assert result == batch
    assert len(session.calls) == 2
    print("✅ test_fetch_retry_on_timeout")


def test_fetch_retry_exhaustion():
    import requests
    session = MockSession([
        requests.exceptions.Timeout("1"),
        requests.exceptions.Timeout("2"),
        requests.exceptions.Timeout("3"),
    ])
    try:
        fetch_binance_klines(
            "BTCUSDT", "1h", session=session,
            backoff_base=0, max_retries=3,
        )
        assert False
    except RuntimeError as e:
        assert "3 attempts" in str(e)
    assert len(session.calls) == 3
    print("✅ test_fetch_retry_exhaustion")


def test_fetch_429_retry():
    ts = 1_700_000_000_000
    batch = _batch(ts, 2)
    session = MockSession([
        MockResponse(429),
        MockResponse(200, json_data=batch),
    ])
    result = fetch_binance_klines(
        "BTCUSDT", "1h", session=session, backoff_base=0,
    )
    assert result == batch
    assert len(session.calls) == 2
    print("✅ test_fetch_429_retry")


def test_fetch_500_retry():
    ts = 1_700_000_000_000
    batch = _batch(ts, 2)
    session = MockSession([
        MockResponse(500),
        MockResponse(200, json_data=batch),
    ])
    result = fetch_binance_klines(
        "BTCUSDT", "1h", session=session, backoff_base=0,
    )
    assert result == batch
    print("✅ test_fetch_500_retry")


def test_fetch_400_no_retry():
    import requests
    session = MockSession([
        MockResponse(
            400,
            raise_for_status_exc=requests.HTTPError("HTTP 400"),
        ),
    ])
    try:
        fetch_binance_klines(
            "BTCUSDT", "1h", session=session, backoff_base=0,
        )
        assert False
    except requests.HTTPError:
        pass
    assert len(session.calls) == 1
    print("✅ test_fetch_400_no_retry")


# ============================================================
# 5. fetch_binance_klines_range (8)
# ============================================================

def test_range_single_page():
    ts = 1_700_000_000_000
    batch = _batch(ts, 6)
    session = MockSession([MockResponse(200, json_data=batch)])
    result = fetch_binance_klines_range(
        "BTCUSDT", "1h",
        start_ms=ts, end_ms=ts + 5 * 3_600_000,
        session=session, backoff_base=0,
        sleep_between_requests=0,
    )
    assert len(result) == 6
    assert int(result[0][0]) == ts
    assert int(result[-1][0]) == ts + 5 * 3_600_000
    # گپ‌های درونی نباید وجود داشته باشد
    assert find_gaps(result, "1h") == []
    print("✅ test_range_single_page")


def test_range_multi_page():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000

    page1 = _batch(ts, MAX_LIMIT)
    next_ts = ts + MAX_LIMIT * interval_ms
    page2 = _batch(next_ts, 5)
    end_ms = next_ts + 5 * interval_ms

    session = MockSession([
        MockResponse(200, json_data=page1),
        MockResponse(200, json_data=page2),
    ])
    result = fetch_binance_klines_range(
        "BTCUSDT", "1h",
        start_ms=ts, end_ms=end_ms,
        session=session, backoff_base=0,
        sleep_between_requests=0,
    )
    assert len(result) == MAX_LIMIT + 5
    assert int(result[0][0]) == ts
    assert int(result[-1][0]) == end_ms
    # پیوستگی داخلی
    assert find_gaps(result, "1h") == [], "unexpected gaps in continuous data"
    print("✅ test_range_multi_page")


def test_range_multi_page_with_gap():
    """
    چند صفحه با گپ 3 کندلی بین دو صفحه.

    قرارداد: fetch_binance_klines_range گپ را رد نمی‌کند.
    تشخیص گپ مسئولیت find_gaps است.
    """
    ts = 1_700_000_000_000
    interval_ms = 3_600_000

    page1 = _batch(ts, MAX_LIMIT)  # ts .. ts+999*int
    # page2 با گپ 3 کندلی شروع می‌شود:
    page2_start = ts + (MAX_LIMIT + 3) * interval_ms
    page2 = _batch(page2_start, 5)  # ts+1003*int .. ts+1007*int

    end_ms = ts + (MAX_LIMIT + 8) * interval_ms

    session = MockSession([
        MockResponse(200, json_data=page1),
        MockResponse(200, json_data=page2),
    ])
    result = fetch_binance_klines_range(
        "BTCUSDT", "1h",
        start_ms=ts, end_ms=end_ms,
        session=session, backoff_base=0,
        sleep_between_requests=0,
    )

    assert len(result) == MAX_LIMIT + 5

    gaps = find_gaps(result, "1h")
    assert len(gaps) == 1, f"expected exactly 1 gap, got {len(gaps)}"
    assert gaps[0]["missing_count"] == 3

    print("✅ test_range_multi_page_with_gap")


def test_range_empty_raises_truncated():
    ts = 1_700_000_000_000
    session = MockSession([MockResponse(200, json_data=[])])
    try:
        fetch_binance_klines_range(
            "BTCUSDT", "1h",
            start_ms=ts, end_ms=ts + 3_600_000,
            session=session, backoff_base=0,
            sleep_between_requests=0,
        )
        assert False
    except TruncatedDataError:
        pass
    print("✅ test_range_empty_raises_truncated")


def test_range_max_candles_truncation():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    batch = _batch(ts, MAX_LIMIT)
    session = MockSession([MockResponse(200, json_data=batch)])
    try:
        fetch_binance_klines_range(
            "BTCUSDT", "1h",
            start_ms=ts, end_ms=ts + 500 * interval_ms,
            max_candles=5,
            session=session, backoff_base=0,
            sleep_between_requests=0,
        )
        assert False
    except TruncatedDataError as e:
        assert "max_candles=5" in str(e)
    print("✅ test_range_max_candles_truncation")


def test_range_max_iterations_truncation():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    page = _batch(ts, MAX_LIMIT)
    session = MockSession([
        MockResponse(200, json_data=page),
        MockResponse(200, json_data=page),
    ])
    try:
        fetch_binance_klines_range(
            "BTCUSDT", "1h",
            start_ms=ts, end_ms=ts + 5000 * interval_ms,
            max_iterations=1,
            session=session, backoff_base=0,
            sleep_between_requests=0,
        )
        assert False
    except TruncatedDataError as e:
        assert "max_iterations=1" in str(e)
    print("✅ test_range_max_iterations_truncation")


def test_range_strict_false_allows_partial():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    batch = _batch(ts, MAX_LIMIT)
    session = MockSession([MockResponse(200, json_data=batch)])
    result = fetch_binance_klines_range(
        "BTCUSDT", "1h",
        start_ms=ts, end_ms=ts + 500 * interval_ms,
        max_candles=5,
        session=session, backoff_base=0,
        sleep_between_requests=0,
        strict_completeness=False,
    )
    assert len(result) <= 5
    print("✅ test_range_strict_false_allows_partial")


def test_range_dedup_and_sort():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    # داده‌ی نا-مرتب با تکراری
    raw = [
        _candle(ts + 2 * interval_ms),
        _candle(ts),
        _candle(ts + 1 * interval_ms),
        _candle(ts),  # duplicate
    ]
    session = MockSession([MockResponse(200, json_data=raw)])
    result = fetch_binance_klines_range(
        "BTCUSDT", "1h",
        start_ms=ts, end_ms=ts + 2 * interval_ms,
        session=session, backoff_base=0,
        sleep_between_requests=0,
    )
    assert len(result) == 3
    assert int(result[0][0]) == ts
    assert int(result[1][0]) == ts + interval_ms
    assert int(result[2][0]) == ts + 2 * interval_ms
    print("✅ test_range_dedup_and_sort")


# ============================================================
# 6. Closed candle (2)
# ============================================================

def test_is_candle_closed():
    ts = 1_700_000_000_000
    assert is_binance_candle_closed(ts, ts + 3_600_000, "1h") is True
    assert is_binance_candle_closed(ts, ts + 3_600_000 - 1, "1h") is False
    print("✅ test_is_candle_closed")


def test_filter_closed():
    ts = 1_700_000_000_000
    now = ts + 2 * 3_600_000 + 100
    candles = [
        _candle(ts),
        _candle(ts + 3_600_000),
        _candle(ts + 2 * 3_600_000),
    ]
    filtered = filter_closed_binance_candles(candles, "1h", now_ms=now)
    assert len(filtered) == 2
    print("✅ test_filter_closed")


# ============================================================
# 7. Gap detection (3)
# ============================================================

def test_find_gaps_none():
    ts = 1_700_000_000_000
    candles = _batch(ts, 5)
    gaps = find_gaps(candles, "1h")
    assert gaps == []
    print("✅ test_find_gaps_none")


def test_find_gaps_one_missing():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    candles = [
        _candle(ts),
        _candle(ts + interval_ms),
        _candle(ts + 3 * interval_ms),
    ]
    gaps = find_gaps(candles, "1h")
    assert len(gaps) == 1
    assert gaps[0]["missing_count"] == 1
    assert gaps[0]["delta_ms"] == 2 * interval_ms
    print("✅ test_find_gaps_one_missing")


def test_find_gaps_five_missing():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    candles = [
        _candle(ts),
        _candle(ts + 6 * interval_ms),
    ]
    gaps = find_gaps(candles, "1h")
    assert len(gaps) == 1
    assert gaps[0]["missing_count"] == 5
    print("✅ test_find_gaps_five_missing")


# ============================================================
# 8. Summarize (3)
# ============================================================

def test_summarize_empty():
    s = summarize_candles([], "1h")
    assert s["count"] == 0
    assert s["gaps"] == []
    assert s["gap_count"] == 0
    print("✅ test_summarize_empty")


def test_summarize_normal():
    ts = 1_700_000_000_000
    candles = _batch(ts, 10)
    s = summarize_candles(candles, "1h")
    assert s["count"] == 10
    assert s["duplicates"] == 0
    assert s["gap_count"] == 0
    assert int(s["first_ts"]) == ts
    print("✅ test_summarize_normal")


def test_summarize_with_gap():
    ts = 1_700_000_000_000
    interval_ms = 3_600_000
    candles = [
        _candle(ts),
        _candle(ts + interval_ms),
        _candle(ts + 3 * interval_ms),
        _candle(ts + 4 * interval_ms),
    ]
    s = summarize_candles(candles, "1h")
    assert s["count"] == 4
    assert s["gap_count"] == 1
    assert s["total_missing"] == 1
    print("✅ test_summarize_with_gap")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Argument validation (5)
        test_validate_args_ok,
        test_validate_args_bad_symbol,
        test_validate_args_bad_interval,
        test_validate_args_bad_limit,
        test_validate_args_bad_retries,
        # Retry policy (1)
        test_should_retry_http,
        # Candle validation (7)
        test_validate_candle_ok,
        test_validate_candle_not_list,
        test_validate_candle_short,
        test_validate_candle_bad_ts,
        test_validate_candle_non_numeric,
        test_validate_candle_negative_volume,
        test_validate_candle_bad_ohlc,
        # fetch_binance_klines (9)
        test_fetch_single_success,
        test_fetch_invalid_response,
        test_fetch_invalid_json,
        test_fetch_invalid_candle_rejected,
        test_fetch_retry_on_timeout,
        test_fetch_retry_exhaustion,
        test_fetch_429_retry,
        test_fetch_500_retry,
        test_fetch_400_no_retry,
        # fetch_binance_klines_range (8)
        test_range_single_page,
        test_range_multi_page,
        test_range_multi_page_with_gap,
        test_range_empty_raises_truncated,
        test_range_max_candles_truncation,
        test_range_max_iterations_truncation,
        test_range_strict_false_allows_partial,
        test_range_dedup_and_sort,
        # Closed candle (2)
        test_is_candle_closed,
        test_filter_closed,
        # Gap detection (3)
        test_find_gaps_none,
        test_find_gaps_one_missing,
        test_find_gaps_five_missing,
        # Summarize (3)
        test_summarize_empty,
        test_summarize_normal,
        test_summarize_with_gap,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Binance Feed Unit Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)