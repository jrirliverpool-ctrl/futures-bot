"""
test_probe_known_gaps.py — Unit tests for v2.2 --known-gaps feature.

مستقل از tests/test_probe_history.py. تمام تست‌ها deterministic.

پنجره‌ی مؤثر (تثبیت‌شده):
  start=00:00, end=10:00 → eff_start=00:00, eff_end=09:00 → expected=10
  (کندل 10:00 در بازه نیست چون end انحصاری است)
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from probe_binance_history import (
    InstrumentedSession,
    KnownGapsError,
    load_known_gaps,
    build_known_ts_set,
    main as probe_main,
)


BASE_TS = 1_698_796_800_000   # 2023-11-01T00:00:00Z (aligned 1h)
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
            raise Exception(f"HTTP {self.status_code}")


class MockSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._responses:
            raise RuntimeError("MockSession exhausted")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _candle(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=1000.0):
    return [ts, str(o), str(h), str(l), str(c), str(v),
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candles_for_hours(hours):
    """کندل‌های ساعت‌های داده‌شده (نسبت به BASE_TS)."""
    return [_candle(BASE_TS + h * HOUR) for h in hours]


def _resp_ok(candles):
    return MockResponse(200, json_data=candles)


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


def _write_known_gaps(td, gaps, symbol="BTCUSDT", interval="1h",
                     schema="1.0.0", filename="kg.json"):
    doc = {
        "schema_version": schema,
        "symbol": symbol,
        "interval": interval,
        "source": "data-api.binance.vision",
        "gaps": gaps,
    }
    p = os.path.join(td, filename)
    Path(p).write_text(json.dumps(doc), encoding="utf-8")
    return p


def _gap(start_iso, end_iso, count, gid=1):
    return {
        "id": gid,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "count": count,
        "cause": "unknown_legacy_gap",
        "cause_confidence": "unverified",
    }


def _iso_hour(h):
    """ISO برای BASE_TS + h ساعت."""
    from datetime import datetime, timezone
    ms = BASE_TS + h * HOUR
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ============================================================
# 1. load_known_gaps — validation (11 tests)
# ============================================================

def test_load_valid():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [_gap(_iso_hour(3), _iso_hour(3), 1)])
        doc = load_known_gaps(p, "BTCUSDT", "1h")
    assert doc["schema_version"] == "1.0.0"
    assert len(doc["gaps"]) == 1
    assert doc["gaps"][0]["count"] == 1


def test_load_missing_file():
    try:
        load_known_gaps("/nonexistent/xyz.json", "BTCUSDT", "1h")
        assert False
    except KnownGapsError as e:
        assert "not found" in str(e)


def test_load_invalid_json():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "bad.json")
        Path(p).write_text("{not json", encoding="utf-8")
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "parse" in str(e).lower() or "JSON" in str(e)


def test_load_schema_mismatch():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [], schema="99.0.0")
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "schema_version" in str(e)


def test_load_symbol_mismatch():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [], symbol="ETHUSDT")
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "symbol" in str(e)


def test_load_interval_mismatch():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [], interval="4h")
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "interval" in str(e)


def test_load_count_mismatch():
    # 03:00 → 05:00 = 3 کندل (03, 04, 05)، declared=2 → خطا
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [_gap(_iso_hour(3), _iso_hour(5), 2)])
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "count" in str(e)


def test_load_misaligned_start():
    # 03:30 نامنطبق با 1h
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [
            _gap("2023-11-01T03:30:00+00:00",
                 "2023-11-01T03:30:00+00:00", 1),
        ])
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "aligned" in str(e) or "interval" in str(e)


def test_load_end_before_start():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [_gap(_iso_hour(5), _iso_hour(3), 1)])
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "end_iso" in str(e) or "start_iso" in str(e)


def test_load_overlapping_ranges():
    """دو بازه‌ی هم‌پوشان → KnownGapsError."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(5), 3, 1),   # 03, 04, 05
            _gap(_iso_hour(5), _iso_hour(7), 3, 2),   # 05, 06, 07 — هم‌پوشان
        ])
        try:
            load_known_gaps(p, "BTCUSDT", "1h")
            assert False
        except KnownGapsError as e:
            assert "overlapping" in str(e).lower()


def test_load_multiple_disjoint_ranges():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(5), 3, 1),
            _gap(_iso_hour(10), _iso_hour(10), 1, 2),
        ])
        doc = load_known_gaps(p, "BTCUSDT", "1h")
    assert len(doc["gaps"]) == 2


# ============================================================
# 2. build_known_ts_set (2 tests)
# ============================================================

def test_build_ts_set_single():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [_gap(_iso_hour(3), _iso_hour(5), 3)])
        doc = load_known_gaps(p, "BTCUSDT", "1h")
    assert len(build_known_ts_set(doc, "1h")) == 3


def test_build_ts_set_multiple():
    with tempfile.TemporaryDirectory() as td:
        p = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(5), 3, 1),
            _gap(_iso_hour(10), _iso_hour(10), 1, 2),
        ])
        doc = load_known_gaps(p, "BTCUSDT", "1h")
    assert len(build_known_ts_set(doc, "1h")) == 4


# ============================================================
# 3. E2E with --known-gaps (8 tests)
# ============================================================

def _base_argv(out=None, known_gaps=None):
    """
    start=00:00، end=10:00 → eff_start=00:00، eff_end=09:00
    → expected_count = 10 کندل (00..09).
    """
    argv = [
        "--symbol", "BTCUSDT", "--interval", "1h",
        "--start", "2023-11-01T00:00:00Z",
        "--end", "2023-11-01T10:00:00Z",
        "--max-candles", "10000",
        "--max-iterations", "100",
        "--max-seconds", "60",
        "--backoff-base", "0",
        "--sleep-between", "0",
    ]
    if known_gaps:
        argv += ["--known-gaps", known_gaps]
    if out:
        argv += ["--out", out]
    return argv


def test_e2e_window_semantics_sanity():
    """تثبیت قرارداد: بازه‌ی مؤثر 00..09 (۱۰ کندل) است، نه 11."""
    session = MockSession([_resp_ok(_candles_for_hours(range(0, 10)))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 0
    assert report["window"]["expected_count"] == 10
    assert report["window"]["effective_start_iso"] == \
        "2023-11-01T00:00:00+00:00"
    assert report["window"]["effective_end_iso"] == \
        "2023-11-01T09:00:00+00:00"


def test_e2e_all_missing_known():
    """
    کندل‌های 03 و 07 مفقود، هر دو در فهرست known.
    expected=10، actual=8، missing=2، missing_unknown=0.
    """
    candles = _candles_for_hours([0, 1, 2, 4, 5, 6, 8, 9])
    session = MockSession([_resp_ok(candles)])

    with tempfile.TemporaryDirectory() as td:
        kg = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(3), 1, 1),
            _gap(_iso_hour(7), _iso_hour(7), 1, 2),
        ])
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out, known_gaps=kg), session)
        report = _read_report(out)

    assert code == 0
    assert report["completeness"] == "complete_with_known_gaps"
    assert report["has_known_gaps"] is True
    assert report["verdict_ok"] is True
    assert report["counts"]["expected"] == 10
    assert report["counts"]["actual_unique"] == 8
    assert report["counts"]["missing"] == 2
    assert report["counts"]["missing_known"] == 2
    assert report["counts"]["missing_unknown"] == 0
    assert report["counts"]["unknown_missing_sample"] == []


def test_e2e_some_missing_unknown_writes_report():
    """
    کندل‌های 03، 05، 07 مفقود؛ فقط 03 و 07 در فهرست.
    missing_unknown=1 → incomplete + code=1 + گزارش نوشته می‌شود.
    """
    candles = _candles_for_hours([0, 1, 2, 4, 6, 8, 9])
    session = MockSession([_resp_ok(candles)])

    with tempfile.TemporaryDirectory() as td:
        kg = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(3), 1, 1),
            _gap(_iso_hour(7), _iso_hour(7), 1, 2),
        ])
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out, known_gaps=kg), session)

        # ← تأکید: گزارش باید حتی در شکست نوشته شود
        assert os.path.exists(out), "report must be written on failure"
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["has_known_gaps"] is True
    assert report["verdict_ok"] is False
    assert report["counts"]["missing"] == 3
    assert report["counts"]["missing_known"] == 2
    assert report["counts"]["missing_unknown"] == 1
    assert len(report["counts"]["unknown_missing_sample"]) == 1
    assert "05:00:00" in report["counts"]["unknown_missing_sample"][0]


def test_e2e_no_known_gaps_no_missing():
    session = MockSession([_resp_ok(_candles_for_hours(range(0, 10)))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 0
    assert report["completeness"] == "complete"
    assert report["has_known_gaps"] is False
    assert report["counts"]["missing"] == 0


def test_e2e_no_known_gaps_with_missing():
    """بدون فهرست، ۱ کندل مفقود → missing_unknown=1 → incomplete."""
    candles = _candles_for_hours([0, 1, 2, 3, 4, 6, 7, 8, 9])
    session = MockSession([_resp_ok(candles)])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out), session)
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["counts"]["missing"] == 1
    assert report["counts"]["missing_known"] == 0
    assert report["counts"]["missing_unknown"] == 1


def test_e2e_invalid_known_gaps_path_records_error():
    """فایل نامعتبر → گزارش با fetch_status=known_gaps_error نوشته شود."""
    session = MockSession([_resp_ok(_candles_for_hours(range(0, 10)))])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(
            _base_argv(out=out, known_gaps="/nonexistent/x.json"),
            session,
        )
        assert os.path.exists(out), "report must be written on known_gaps error"
        report = _read_report(out)

    assert code == 1
    assert report["fetch_status"] == "known_gaps_error"
    assert report["verdict_ok"] is False
    assert report["completeness"] == "unknown"
    assert "KnownGapsError" in report["fetch_error"]


def test_e2e_known_gaps_symbol_mismatch_records_error():
    session = MockSession([_resp_ok(_candles_for_hours(range(0, 10)))])
    with tempfile.TemporaryDirectory() as td:
        kg = _write_known_gaps(td, [], symbol="ETHUSDT")
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out, known_gaps=kg), session)
        report = _read_report(out)

    assert code == 1
    assert report["fetch_status"] == "known_gaps_error"
    assert "symbol" in report["fetch_error"].lower()


def test_e2e_known_gaps_partial_coverage():
    """
    ۵ کندل مفقود پیوسته (03..07)؛ فقط 03 و 04 در فهرست.
    missing_known=2، missing_unknown=3 → incomplete.
    """
    candles = _candles_for_hours([0, 1, 2, 8, 9])
    session = MockSession([_resp_ok(candles)])

    with tempfile.TemporaryDirectory() as td:
        kg = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(4), 2, 1),
        ])
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out, known_gaps=kg), session)
        report = _read_report(out)

    assert code == 1
    assert report["completeness"] == "incomplete"
    assert report["counts"]["missing"] == 5
    assert report["counts"]["missing_known"] == 2
    assert report["counts"]["missing_unknown"] == 3


def test_e2e_report_includes_known_gaps_block():
    session = MockSession([_resp_ok(_candles_for_hours(range(0, 10)))])
    with tempfile.TemporaryDirectory() as td:
        kg = _write_known_gaps(td, [
            _gap(_iso_hour(3), _iso_hour(3), 1),
        ])
        out = os.path.join(td, "r.json")
        code, _ = _run_probe(_base_argv(out=out, known_gaps=kg), session)
        report = _read_report(out)

    assert "known_gaps" in report
    block = report["known_gaps"]
    assert block["schema_version"] == "1.0.0"
    assert block["symbol"] == "BTCUSDT"
    assert block["interval"] == "1h"
    assert block["gaps_total"] == 1
    assert block["candles_total"] == 1


# ============================================================
# 4. Real reference file — STRICT (1 test)
# ============================================================

def test_real_known_gaps_file_loads_strict():
    """
    فایل مرجع واقعی باید وجود داشته باشد.
    در CI مخزن اصلی، نبودن فایل → شکست تست (نه skip).
    """
    repo_root = Path(__file__).resolve().parent.parent
    real_path = repo_root / "data" / "known_gaps_btcusdt_1h.json"
    assert real_path.exists(), (
        f"reference file must exist in repo: {real_path}"
    )

    doc = load_known_gaps(str(real_path), "BTCUSDT", "1h")
    assert doc["schema_version"] == "1.0.0"
    assert doc["symbol"] == "BTCUSDT"
    assert doc["interval"] == "1h"
    assert len(doc["gaps"]) == 20

    ts_set = build_known_ts_set(doc, "1h")
    assert len(ts_set) == 60


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # load_known_gaps (11)
        test_load_valid,
        test_load_missing_file,
        test_load_invalid_json,
        test_load_schema_mismatch,
        test_load_symbol_mismatch,
        test_load_interval_mismatch,
        test_load_count_mismatch,
        test_load_misaligned_start,
        test_load_end_before_start,
        test_load_overlapping_ranges,
        test_load_multiple_disjoint_ranges,
        # build_known_ts_set (2)
        test_build_ts_set_single,
        test_build_ts_set_multiple,
        # e2e (8)
        test_e2e_window_semantics_sanity,
        test_e2e_all_missing_known,
        test_e2e_some_missing_unknown_writes_report,
        test_e2e_no_known_gaps_no_missing,
        test_e2e_no_known_gaps_with_missing,
        test_e2e_invalid_known_gaps_path_records_error,
        test_e2e_known_gaps_symbol_mismatch_records_error,
        test_e2e_known_gaps_partial_coverage,
        test_e2e_report_includes_known_gaps_block,
        # real file (1)
        test_real_known_gaps_file_loads_strict,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print(f"🧪 Known Gaps Unit Tests ({len(tests)} total)")
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