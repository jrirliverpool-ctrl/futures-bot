"""
test_probe_history.py — Unit tests for scripts/probe_binance_history.py.

تمام تست‌ها deterministic و بدون شبکه هستند.
هیچ درخواست واقعی به Binance ارسال نمی‌شود.

مجموع: ۴۷ تست
  - Time helpers (6)
  - resolve_window (6)
  - analyze_timestamps (6)
  - InstrumentedSession (8)
  - classify_attempts (7)
  - End-to-end (14)
"""

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import requests

from binance_feed import INTERVAL_MS

from probe_binance_history import (
    InstrumentedSession,
    DeadlineExceeded,
    RateLimitBan,
    _parse_iso_utc,
    _floor_to_interval,
    _ceil_to_interval,
    _iso,
    resolve_window,
    analyze_timestamps,
    classify_attempts,
    main as probe_main,
)


# ============================================================
# Fixtures
# ============================================================

BASE_TS = 1_698_796_800_000   # 2023-11-01T00:00:00Z (aligned to 1h)
HOUR = 3_600_000


# ============================================================
# Mock infrastructure
# ============================================================

class MockResponse:
    def __init__(self, status, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data if json_data is not None else []
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class MockSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._responses:
            raise RuntimeError(
                f"MockSession exhausted at call #{len(self.calls)}"
            )
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ============================================================
# Candle helpers
# ============================================================

def _candle(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=1000.0):
    return [
        ts,
        str(o), str(h), str(l), str(c), str(v),
        ts + HOUR - 1,
        "0", 0, "0", "0", "0",
    ]


def _batch(start_ts, n):
    return [_candle(start_ts + i * HOUR) for i in range(n)]


def _resp_ok(candles):
    return MockResponse(200, json_data=candles)


# ============================================================
# Runner — تزریق MockSession به InstrumentedSession واقعی
# ============================================================

def _run_probe(argv, mock_session):
    orig_init = InstrumentedSession.__init__

    def patched_init(self, inner=None, **kw):
        orig_init(self, inner=mock_session, **kw)

    buf = io.StringIO()
    with patch.object(InstrumentedSession, "__init__", patched_init):
        with redirect_stdout(buf):
            code = probe_main(argv)
    return code, buf.getvalue()


def _read_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 1. Time helpers (6)
# ============================================================

def test_parse_iso_z():
    assert _parse_iso_utc("2020-01-01T00:00:00Z") == 1_577_836_800_000


def test_parse_iso_offset():
    assert _parse_iso_utc("2020-01-01T00:00:00+00:00") == 1_577_836_800_000


def test_parse_iso_naive():
    assert _parse_iso_utc("2020-01-01T00:00:00") == 1_577_836_800_000


def test_floor_to_interval():
    ms = 1_700_000_123_456
    f = _floor_to_interval(ms, HOUR)
    assert f % HOUR == 0
    assert f <= ms < f + HOUR


def test_ceil_to_interval():
    ms = 1_700_000_123_456
    c = _ceil_to_interval(ms, HOUR)
    assert c % HOUR == 0
    assert c >= ms
    assert c - HOUR < ms
    assert _ceil_to_interval(BASE_TS, HOUR) == BASE_TS


def test_iso_format():
    assert _iso(BASE_TS) == "2023-11-01T00:00:00+00:00"


# ============================================================
# 2. resolve_window (6)
# ============================================================

def test_resolve_window_ceil_unaligned():
    now_ms = 1_700_000_000_000
    win = resolve_window(
        "2023-11-01T00:30:00Z", "2023-11-01T10:00:00Z", "1h",
        now_ms=now_ms, align="ceil",
    )
    assert win["eff_start_ms"] == BASE_TS + HOUR
    assert win["align"] == "ceil"
    assert win["requested_start_ms"] == BASE_TS + HOUR // 2


def test_resolve_window_floor_unaligned():
    now_ms = 1_700_000_000_000
    win = resolve_window(
        "2023-11-01T00:30:00Z", "2023-11-01T10:00:00Z", "1h",
        now_ms=now_ms, align="floor",
    )
    assert win["eff_start_ms"] == BASE_TS
    assert win["align"] == "floor"


def test_resolve_window_aligned_no_op():
    now_ms = 1_700_000_000_000
    for align in ("floor", "ceil"):
        win = resolve_window(
            "2023-11-01T00:00:00Z", "2023-11-01T10:00:00Z", "1h",
            now_ms=now_ms, align=align,
        )
        assert win["eff_start_ms"] == BASE_TS


def test_resolve_window_caps_at_now():
    now_ms = 1_700_000_000_000
    win = resolve_window(
        "2023-01-01T00:00:00Z", "2030-01-01T00:00:00Z", "1h",
        now_ms=now_ms,
    )
    assert win["eff_end_ms"] + HOUR <= now_ms


def test_resolve_window_reversed_raises():
    try:
        resolve_window(
            "2030-01-01T00:00:00Z", "2020-01-01T00:00:00Z", "1h",
            now_ms=1_700_000_000_000,
        )
        assert False
    except ValueError:
        pass


def test_resolve_window_different_intervals():
    now_ms = 1_700_000_000_000
    for interval in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
        win = resolve_window(
            "2023-01-01T00:00:00Z", None, interval,
            now_ms=now_ms, align="ceil",
        )
        int_ms = INTERVAL_MS[interval]
        assert win["eff_start_ms"] % int_ms == 0
        assert win["eff_end_ms"] % int_ms == 0
        assert win["eff_end_ms"] + int_ms <= now_ms
        expected = (
            (win["eff_end_ms"] - win["eff_start_ms"]) // int_ms + 1
        )
        assert win["expected_count"] == expected


# ============================================================
# 3. analyze_timestamps (6)
# ============================================================

def test_analyze_clean():
    candles = _batch(BASE_TS, 10)
    res = analyze_timestamps(
        candles, BASE_TS, BASE_TS + 9 * HOUR, "1h",
    )
    assert res["missing_count"] == 0
    assert res["duplicates"] == 0
    assert res["extra_count"] == 0
    assert res["actual_unique"] == 10


def test_analyze_one_missing():
    candles = [
        c for c in _batch(BASE_TS, 10)
        if int(c[0]) != BASE_TS + 3 * HOUR
    ]
    res = analyze_timestamps(
        candles, BASE_TS, BASE_TS + 9 * HOUR, "1h",
    )
    assert res["missing_count"] == 1
    assert res["missing_ranges_total"] == 1
    assert res["missing_ranges_sample"][0]["count"] == 1


def test_analyze_multiple_ranges():
    candles = [
        c for c in _batch(BASE_TS, 10)
        if int(c[0]) not in (BASE_TS + 2 * HOUR, BASE_TS + 5 * HOUR)
    ]
    res = analyze_timestamps(
        candles, BASE_TS, BASE_TS + 9 * HOUR, "1h",
    )
    assert res["missing_count"] == 2
    assert res["missing_ranges_total"] == 2


def test_analyze_duplicates():
    candles = _batch(BASE_TS, 10)
    candles.append(_candle(BASE_TS))
    res = analyze_timestamps(
        candles, BASE_TS, BASE_TS + 9 * HOUR, "1h",
    )
    assert res["duplicates"] == 1
    assert res["actual_unique"] == 10
    assert res["missing_count"] == 0


def test_analyze_out_of_range():
    candles = _batch(BASE_TS, 5)
    candles.append(_candle(BASE_TS + 100 * HOUR))
    res = analyze_timestamps(
        candles, BASE_TS, BASE_TS + 4 * HOUR, "1h",
    )
    assert res["extra_count"] == 1
    assert res["actual_unique"] == 5
    assert res["missing_count"] == 0


def test_analyze_misaligned():
    candles = _batch(BASE_TS, 3)
    candles.append(_candle(BASE_TS + HOUR // 2))
    res = analyze_timestamps(
        candles, BASE_TS, BASE_TS + 2 * HOUR, "1h",
    )
    assert res["extra_count"] == 1
    assert res["actual_unique"] == 3
    assert res["missing_count"] == 0


# ============================================================
# 4. InstrumentedSession (8)
# ============================================================

def test_session_records_success():
    inner = MockSession([MockResponse(200, json_data=[])])
    s = InstrumentedSession(inner=inner)
    s.get("http://x", params={"a": 1}, timeout=5)
    assert len(s.records) == 1
    assert s.records[0]["status"] == 200
    assert s.records[0]["outcome"] == "ok"
    assert s.records[0]["params"] == {"a": 1}
    assert s.aborted is False
    assert inner.calls[0]["timeout"] == 5


def test_session_418_raises_immediately():
    inner = MockSession([
        MockResponse(418),
        MockResponse(200, json_data=[]),
    ])
    s = InstrumentedSession(inner=inner)

    try:
        s.get("http://x")
        assert False, "expected RateLimitBan"
    except RateLimitBan:
        pass

    assert len(s.records) == 1
    assert s.records[0]["status"] == 418
    assert s.aborted is True
    assert s.abort_reason == "418 received"

    try:
        s.get("http://y")
        assert False, "expected RateLimitBan on subsequent call"
    except RateLimitBan:
        pass

    # مهم: هیچ درخواست دومی به inner ارسال نشده است
    assert len(inner.calls) == 1


def test_session_timeout_records_and_raises():
    inner = MockSession([requests.exceptions.Timeout("t")])
    s = InstrumentedSession(inner=inner)
    try:
        s.get("http://x", timeout=1)
        assert False
    except requests.exceptions.Timeout:
        pass
    assert len(s.records) == 1
    assert s.records[0]["outcome"] == "Timeout"
    assert s.records[0]["status"] is None


def test_session_retry_after_captured():
    inner = MockSession([
        MockResponse(429, headers={"Retry-After": "30"}),
    ])
    s = InstrumentedSession(inner=inner)
    s.get("http://x")
    assert s.records[0]["retry_after"] == "30"


def test_session_deadline_immediate():
    past = time.monotonic() - 1
    s = InstrumentedSession(
        inner=MockSession([]), deadline_monotonic=past,
    )
    try:
        s.get("http://x", timeout=10)
        assert False
    except DeadlineExceeded:
        pass
    assert s.aborted is True
    assert s.abort_reason == "max_seconds exceeded"


def test_session_min_window_boundary():
    deadline = time.monotonic() + 0.2
    s = InstrumentedSession(
        inner=MockSession([]), deadline_monotonic=deadline,
        min_request_window_s=0.5,
    )
    try:
        s.get("http://x", timeout=10)
        assert False
    except DeadlineExceeded:
        pass


def test_session_timeout_capped_by_deadline():
    inner = MockSession([MockResponse(200, json_data=[])])
    deadline = time.monotonic() + 1.0
    s = InstrumentedSession(
        inner=inner, deadline_monotonic=deadline,
        min_request_window_s=0.1,
    )
    s.get("http://x", timeout=100)
    passed = inner.calls[0]["timeout"]
    assert 0.0 < passed <= 1.0


def test_session_no_deadline_passes_timeout_through():
    inner = MockSession([MockResponse(200, json_data=[])])
    s = InstrumentedSession(inner=inner, deadline_monotonic=None)
    s.get("http://x", timeout=7)
    assert inner.calls[0]["timeout"] == 7


# ============================================================
# 5. classify_attempts (7)
# ============================================================

def _rec(params, outcome="ok", status=200, retry_after=None):
    return {
        "url": "u",
        "params": params,
        "outcome": outcome,
        "status": status,
        "elapsed_ms": 1.0,
        "retry_after": retry_after,
    }


def test_classify_empty():
    s = classify_attempts([])
    assert s["attempts_total"] == 0
    assert s["logical_calls"] == 0
    assert s["retries_total"] == 0
    assert s["method"].startswith("estimated")


def test_classify_single_success():
    s = classify_attempts([_rec({"a": 1})])
    assert s["attempts_success"] == 1
    assert s["logical_calls"] == 1
    assert s["retries_total"] == 0


def test_classify_429_then_success():
    s = classify_attempts([
        _rec({"a": 1}, status=429),
        _rec({"a": 1}, status=200),
    ])
    assert s["logical_calls"] == 1
    assert s["retries_total"] == 1
    assert s["retries_by_cause"]["429"] == 1


def test_classify_5xx_exhaustion():
    s = classify_attempts([
        _rec({"a": 1}, status=500),
        _rec({"a": 1}, status=502),
        _rec({"a": 1}, status=503),
    ])
    assert s["logical_calls"] == 1
    assert s["attempts_failed_5xx"] == 3
    assert s["retries_total"] == 2


def test_classify_two_pages():
    s = classify_attempts([
        _rec({"startTime": 1}),
        _rec({"startTime": 2}),
    ])
    assert s["logical_calls"] == 2
    assert s["retries_total"] == 0


def test_classify_mixed_causes():
    s = classify_attempts([
        _rec({"a": 1}, status=429),
        _rec({"a": 1}, outcome="Timeout", status=None),
        _rec({"a": 1}, status=200),
    ])
    assert s["logical_calls"] == 1
    assert s["retries_total"] == 2
    assert s["retries_by_cause"]["429"] == 1
    assert s["retries_by_cause"]["timeout"] == 1


def test_classify_connection_error_as_timeout():
    s = classify_attempts([
        _rec({"a": 1}, outcome="ConnectionError", status=None),
        _rec({"a": 1}, status=200),
    ])
    assert s["retries_total"] == 1
    assert s["retries_by_cause"]["timeout"] == 1


# ============================================================
# 6. End-to-end (14)
# ============================================================

def _base_argv(start="2023-11-01T00:00:00Z",
               end="2023-11-01T10:00:00Z",
               out=None,
               extra=None):
    argv = [
        "--symbol", "BTCUSDT", "--interval", "1h",
        "--start", start, "--end", end,
        "--max-candles", "10000",
        "--max-iterations", "100",
        "--max-seconds", "60",
        "--backoff-base", "0",
        "--sleep-between", "0",
    ]
    if out:
        argv += ["--out", out]
    if extra:
        argv += extra
    return argv


def test_e2e_complete():
    session = MockSession([_resp_ok(_batch(BASE_TS, 10))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 0
    assert report["verdict_ok"] is True
    assert report["fetch_status"] == "ok"
    assert report["completeness"] == "complete"
    assert report["truncation_reason"] is None
    assert report["counts"]["expected"] == 10
    assert report["counts"]["actual_unique"] == 10
    assert report["counts"]["missing"] == 0


def test_e2e_incomplete():
    session = MockSession([_resp_ok(_batch(BASE_TS, 8))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["counts"]["expected"] == 10
    assert report["counts"]["actual_unique"] == 8
    assert report["counts"]["missing"] == 2


def test_e2e_truncated_max_candles_report():
    session = MockSession([_resp_ok(_batch(BASE_TS, 1000))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(
            end="2024-01-01T00:00:00Z",
            out=out,
            extra=["--max-candles", "5"],
        )
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["truncation_reason"] == "max_candles"
    assert report["counts"]["actual_unique"] == 5


def test_e2e_truncated_max_iterations():
    session = MockSession([_resp_ok(_batch(BASE_TS, 1000))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(
            end="2025-01-01T00:00:00Z",
            out=out,
            extra=[
                "--max-candles", "20000",
                "--max-iterations", "1",
            ],
        )
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["truncation_reason"] == "max_iterations"


def test_e2e_early_stop():
    session = MockSession([_resp_ok(_batch(BASE_TS, 5))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["truncation_reason"] == "early_stop"


def test_e2e_at_cap_but_complete():
    session = MockSession([_resp_ok(_batch(BASE_TS, 5))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(
            end="2023-11-01T05:00:00Z",
            out=out,
            extra=["--max-candles", "5"],
        )
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 0
    assert report["completeness"] == "complete"
    assert report["truncation_reason"] is None
    assert report["counts"]["missing"] == 0


def test_e2e_max_iterations_at_boundary_still_complete():
    """
    مرز دقیق: max_iterations=1 و یک صفحه‌ی ۱۰۰۰ کندلی
    دقیقاً کافی است تا به eff_end برسیم. نباید truncated شود.
    """
    end_iso = _iso(BASE_TS + 1000 * HOUR)

    # تثبیت پیش‌فرض: عدد مورد انتظار باید ۱۰۰۰ باشد
    win = resolve_window(
        start_iso="2023-11-01T00:00:00Z",
        end_iso=end_iso,
        interval="1h",
        now_ms=BASE_TS + 2000 * HOUR,
    )
    assert win["expected_count"] == 1000, (
        f"expected_count drifted: {win['expected_count']}"
    )
    assert win["eff_end_ms"] == BASE_TS + 999 * HOUR

    session = MockSession([_resp_ok(_batch(BASE_TS, 1000))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(
            end=end_iso,
            out=out,
            extra=[
                "--max-candles", "2000",
                "--max-iterations", "1",
            ],
        )
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 0
    assert report["completeness"] == "complete"
    assert report["truncation_reason"] is None
    assert report["counts"]["expected"] == 1000
    assert report["counts"]["actual_unique"] == 1000
    assert report["http"]["logical_calls"] == 1


def test_e2e_418_aborts_no_further_requests():
    session = MockSession([
        MockResponse(418),
        MockResponse(200, json_data=_batch(BASE_TS, 10)),
    ])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 1
    assert report["fetch_status"] == "aborted_418"
    assert report["completeness"] == "unknown"
    assert report["verdict_ok"] is False
    # مهم: فقط یک درخواست به inner رسیده
    assert len(session.calls) == 1
    assert report["http"]["abort_reason"] == "418 received"


def test_e2e_deadline_aborts():
    session = MockSession([_resp_ok(_batch(BASE_TS, 10))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(out=out, extra=["--max-seconds", "0"])
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 1
    assert report["fetch_status"] == "aborted_deadline"
    assert report["completeness"] == "unknown"
    assert len(session.calls) == 0


def test_e2e_empty_response_incomplete():
    session = MockSession([MockResponse(200, json_data=[])])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 1
    assert report["fetch_status"] == "ok"
    assert report["completeness"] == "incomplete"
    assert report["verdict_ok"] is False
    assert report["counts"]["actual_unique"] == 0
    assert report["counts"]["missing"] == 10


def test_e2e_report_write_error():
    session = MockSession([_resp_ok(_batch(BASE_TS, 10))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        with patch.object(
            Path, "write_text",
            side_effect=PermissionError("denied"),
        ):
            code, stdout = _run_probe(_base_argv(out=out), session)

    assert code == 1
    assert "failed to write report" in stdout


def test_e2e_retry_429_then_success():
    session = MockSession([
        MockResponse(429),
        _resp_ok(_batch(BASE_TS, 10)),
    ])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 0
    assert report["completeness"] == "complete"
    http = report["http"]
    assert http["attempts_total"] == 2
    assert http["retries_total"] == 1
    assert http["retries_by_cause"]["429"] == 1
    assert http["logical_calls"] == 1
    assert len(session.calls) == 2


def test_e2e_align_ceil():
    session = MockSession([_resp_ok(_batch(BASE_TS + HOUR, 9))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(
            start="2023-11-01T00:30:00Z",
            out=out,
            extra=["--align", "ceil"],
        )
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 0
    assert report["window"]["align"] == "ceil"
    assert report["window"]["effective_start_iso"] == \
        "2023-11-01T01:00:00+00:00"
    assert report["window"]["requested_start_iso"] == \
        "2023-11-01T00:30:00+00:00"


def test_e2e_align_floor():
    session = MockSession([_resp_ok(_batch(BASE_TS, 10))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        argv = _base_argv(
            start="2023-11-01T00:30:00Z",
            out=out,
            extra=["--align", "floor"],
        )
        code, _ = _run_probe(argv, session)
        report = _read_report(out)

    assert code == 0
    assert report["window"]["align"] == "floor"
    assert report["window"]["effective_start_iso"] == \
        "2023-11-01T00:00:00+00:00"


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Time helpers (6)
        test_parse_iso_z,
        test_parse_iso_offset,
        test_parse_iso_naive,
        test_floor_to_interval,
        test_ceil_to_interval,
        test_iso_format,
        # resolve_window (6)
        test_resolve_window_ceil_unaligned,
        test_resolve_window_floor_unaligned,
        test_resolve_window_aligned_no_op,
        test_resolve_window_caps_at_now,
        test_resolve_window_reversed_raises,
        test_resolve_window_different_intervals,
        # analyze_timestamps (6)
        test_analyze_clean,
        test_analyze_one_missing,
        test_analyze_multiple_ranges,
        test_analyze_duplicates,
        test_analyze_out_of_range,
        test_analyze_misaligned,
        # InstrumentedSession (8)
        test_session_records_success,
        test_session_418_raises_immediately,
        test_session_timeout_records_and_raises,
        test_session_retry_after_captured,
        test_session_deadline_immediate,
        test_session_min_window_boundary,
        test_session_timeout_capped_by_deadline,
        test_session_no_deadline_passes_timeout_through,
        # classify_attempts (7)
        test_classify_empty,
        test_classify_single_success,
        test_classify_429_then_success,
        test_classify_5xx_exhaustion,
        test_classify_two_pages,
        test_classify_mixed_causes,
        test_classify_connection_error_as_timeout,
        # End-to-end (14)
        test_e2e_complete,
        test_e2e_incomplete,
        test_e2e_truncated_max_candles_report,
        test_e2e_truncated_max_iterations,
        test_e2e_early_stop,
        test_e2e_at_cap_but_complete,
        test_e2e_max_iterations_at_boundary_still_complete,
        test_e2e_418_aborts_no_further_requests,
        test_e2e_deadline_aborts,
        test_e2e_empty_response_incomplete,
        test_e2e_report_write_error,
        test_e2e_retry_429_then_success,
        test_e2e_align_ceil,
        test_e2e_align_floor,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print(f"🧪 Probe History Unit Tests ({len(tests)} total)")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
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