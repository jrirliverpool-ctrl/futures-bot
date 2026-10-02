"""
test_data_feed.py — Unit tests for data_feed.py.

تمام تست‌های این فایل deterministic و مستقل از شبکه هستند.
Integration Test جداگانه در فایل دیگری اجرا می‌شود.
"""

import os
import sys
from unittest.mock import Mock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import requests
from data_feed import (
    fetch_klines,
    is_candle_closed,
    filter_closed_candles,
    candle_ts_to_iso,
    INTERVAL_MS,
    SUPPORTED_INTERVALS,
)


def _candle(ts, o=100, h=101, l=99, c=100):
    return [ts, str(o), str(h), str(l), str(c), "1000"]


def _response(status=200, data=None):
    r = Mock()
    r.status_code = status
    if data is None:
        data = [_candle(1_700_000_000_000)]
    r.json.return_value = data
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return r


# ============================================================
# Pure logic
# ============================================================

def test_interval_ms_constants():
    assert INTERVAL_MS["1m"] == 60_000
    assert INTERVAL_MS["1h"] == 3_600_000
    assert INTERVAL_MS["4h"] == 14_400_000
    assert "1h" in SUPPORTED_INTERVALS


def test_is_candle_closed_true():
    ts = 1_700_000_000_000
    now = ts + 2 * INTERVAL_MS["1h"]
    assert is_candle_closed(ts, now, "1h") is True


def test_is_candle_closed_exact_boundary():
    ts = 1_700_000_000_000
    now = ts + INTERVAL_MS["1h"]
    assert is_candle_closed(ts, now, "1h") is True


def test_is_candle_closed_false():
    ts = 1_700_000_000_000
    now = ts + INTERVAL_MS["1h"] - 1
    assert is_candle_closed(ts, now, "1h") is False


def test_is_candle_closed_invalid_interval():
    try:
        is_candle_closed(1_000_000, 2_000_000, "2h")
        assert False
    except ValueError:
        pass


def test_filter_closed_candles_mixed():
    now = 100_000_000
    candles = [
        _candle(now - 300_000),
        _candle(now - 120_000),
        _candle(now - 30_000),
        _candle(now + 10_000),
    ]
    filtered = filter_closed_candles(candles, "1m", now_ms=now)
    assert len(filtered) == 2


def test_filter_preserves_order():
    now = 100_000_000
    candles = [
        _candle(now - 300_000),
        _candle(now - 120_000),
    ]
    filtered = filter_closed_candles(candles, "1m", now_ms=now)
    assert [c[0] for c in filtered] == [now - 300_000, now - 120_000]


def test_filter_future_candle():
    now = 100_000_000
    candles = [_candle(now + 1_000)]
    assert filter_closed_candles(candles, "1m", now_ms=now) == []


def test_candle_ts_to_iso():
    iso = candle_ts_to_iso(1_700_000_000_000)
    assert "2023-11-14" in iso
    assert iso.endswith("+00:00")


# ============================================================
# Argument validation
# ============================================================

def test_fetch_invalid_interval():
    try:
        fetch_klines("BTC-SWAP-USDT", "2h", 10)
        assert False
    except ValueError as exc:
        assert "Unsupported interval" in str(exc)


def test_fetch_invalid_limit():
    for limit in [0, -1, 1001]:
        try:
            fetch_klines("BTC-SWAP-USDT", "1h", limit)
            assert False
        except ValueError:
            pass


def test_fetch_invalid_limit_type():
    for limit in [1.5, True, "10"]:
        try:
            fetch_klines("BTC-SWAP-USDT", "1h", limit)
            assert False
        except ValueError:
            pass


# ============================================================
# Retry logic (mocked, no network)
# ============================================================

@patch("data_feed.time.sleep")
def test_retry_then_success(mock_sleep):
    session = Mock()
    session.get.side_effect = [
        requests.exceptions.Timeout("timeout"),
        _response(200),
    ]
    data = fetch_klines(
        "BTC-SWAP-USDT", "1h", limit=10,
        max_retries=3, backoff_base=1, session=session,
    )
    assert isinstance(data, list)
    assert session.get.call_count == 2
    mock_sleep.assert_called_once_with(1)


@patch("data_feed.time.sleep")
def test_retry_exhaustion(mock_sleep):
    session = Mock()
    session.get.side_effect = [
        requests.exceptions.Timeout("1"),
        requests.exceptions.Timeout("2"),
        requests.exceptions.Timeout("3"),
    ]
    try:
        fetch_klines(
            "BTC-SWAP-USDT", "1h", limit=10,
            max_retries=3, backoff_base=1, session=session,
        )
        assert False
    except RuntimeError as exc:
        assert "3 attempts" in str(exc)
    assert session.get.call_count == 3
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[0].args[0] == 1
    assert mock_sleep.call_args_list[1].args[0] == 2


@patch("data_feed.time.sleep")
def test_http_429_retries(mock_sleep):
    session = Mock()
    session.get.side_effect = [
        _response(429),
        _response(200),
    ]
    data = fetch_klines(
        "BTC-SWAP-USDT", "1h",
        session=session, backoff_base=2,
    )
    assert isinstance(data, list)
    assert session.get.call_count == 2
    mock_sleep.assert_called_once_with(2)


@patch("data_feed.time.sleep")
def test_http_500_retries(mock_sleep):
    session = Mock()
    session.get.side_effect = [
        _response(500),
        _response(502),
        _response(200),
    ]
    data = fetch_klines(
        "BTC-SWAP-USDT", "1h",
        session=session, backoff_base=1,
    )
    assert isinstance(data, list)
    assert session.get.call_count == 3
    assert mock_sleep.call_count == 2


@patch("data_feed.time.sleep")
def test_http_400_does_not_retry(mock_sleep):
    session = Mock()
    session.get.return_value = _response(400)
    try:
        fetch_klines("BTC-SWAP-USDT", "1h", session=session)
        assert False
    except requests.HTTPError:
        pass
    assert session.get.call_count == 1
    mock_sleep.assert_not_called()


# ============================================================
# Response structure
# ============================================================

def test_invalid_json_structure():
    session = Mock()
    r = _response(200, data={"unexpected": "object"})
    session.get.return_value = r
    try:
        fetch_klines("BTC-SWAP-USDT", "1h", session=session)
        assert False
    except ValueError as exc:
        assert "Unexpected response type" in str(exc)


def test_no_network_unit_dependency():
    """تست اطمینان از اینکه fetch با session تزریقی کار می‌کند."""
    session = Mock()
    session.get.return_value = _response(
        200, data=[_candle(1), _candle(2)],
    )
    result = fetch_klines("BTC-SWAP-USDT", "1h", session=session)
    assert len(result) == 2
    session.get.assert_called_once()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_interval_ms_constants,
        test_is_candle_closed_true,
        test_is_candle_closed_exact_boundary,
        test_is_candle_closed_false,
        test_is_candle_closed_invalid_interval,
        test_filter_closed_candles_mixed,
        test_filter_preserves_order,
        test_filter_future_candle,
        test_candle_ts_to_iso,
        test_fetch_invalid_interval,
        test_fetch_invalid_limit,
        test_fetch_invalid_limit_type,
        test_retry_then_success,
        test_retry_exhaustion,
        test_http_429_retries,
        test_http_500_retries,
        test_http_400_does_not_retry,
        test_invalid_json_structure,
        test_no_network_unit_dependency,
    ]

    passed = 0
    failed = 0

    print("=" * 60)
    print("🧪 Data Feed Unit Tests")
    print("=" * 60)

    for test in tests:
        try:
            test()
            print(f"✅ {test.__name__}")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {test.__name__}: {exc}")
            traceback.print_exc()
            failed += 1

    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)

    if failed:
        sys.exit(1)