"""
test_reconciler.py — Unit tests for reconciler.py.

پوشش (12 تست):
  - state missing → ERROR (check دقیق)
  - state invalid → ERROR (check دقیق)
  - hash mismatch → ERROR (check دقیق)
  - hash valid (صراحتاً assert می‌شود)
  - valid state → OK
  - last_processed = None → WARNING (check دقیق)
  - last_processed آینده → ERROR (نسبی، نه ثابت)
  - klines ناکافی → WARNING فقط (check دقیق)
  - gap دقیقاً 2 کندل → WARNING
  - gap دقیقاً 1 کندل → OK (boundary)
  - read-only guarantee
  - deterministic (کل output)

قراردادها که تست بر پایه‌ی آن‌ها ساخته شده:
  - GAP_WARNING_THRESHOLD = 2 (missing >= 2 → WARNING)
  - missing = (delta // interval_ms) - 1
  - state["strategy_hash"] توسط default_state() ست می‌شود
  - now_ms در reconciler نسبی است (نه time.time())
"""

import os
import sys
import shutil
import tempfile
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from reconciler import (
    reconcile_public,
    STATUS_OK,
    STATUS_WARNING,
    STATUS_ERROR,
    GAP_WARNING_THRESHOLD,
    MIN_KLINES,
)
from paper_state_manager import save_state
from state_manager import default_state
from compute_hash import compute_strategy_hash


INTERVAL_MS = 3_600_000   # 1h
BASE_TS = 1_700_000_000_000


# ============================================================
# Helpers
# ============================================================

def _tmp_dir():
    return tempfile.mkdtemp()


def _fake_klines(n, start_ts=BASE_TS, interval_ms=INTERVAL_MS):
    return [
        [start_ts + i * interval_ms,
         "100.0", "101.0", "99.0", "100.5", "1000"]
        for i in range(n)
    ]


def _valid_state(lpct):
    """
    state معتبر.
    default_state() خودش strategy_hash را با compute_strategy_hash() ست می‌کند.
    """
    s = default_state(1000.0)
    s["symbol"] = "BTC-SWAP-USDT"
    s["last_processed_candle_ts"] = lpct
    return s


def _find_check(result, name):
    for c in result["checks"]:
        if c["name"] == name:
            return c
    return None


def _all_failed_checks(result):
    return [c for c in result["checks"] if not c["ok"]]


def _errors(result):
    return [c for c in result["checks"]
            if not c["ok"] and c["level"] == "error"]


def _warnings(result):
    return [c for c in result["checks"]
            if not c["ok"] and c["level"] == "warning"]


# ============================================================
# 1. state missing → ERROR
# ============================================================

def test_state_missing_returns_error():
    d = _tmp_dir()
    try:
        result = reconcile_public(d, "BTC-SWAP-USDT", "1h")
        assert result["status"] == STATUS_ERROR

        chk = _find_check(result, "state_exists")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "error"

        # چون state نیست، فقط همین یک check اجرا می‌شود
        assert len(result["checks"]) == 1
        print("✅ test_state_missing_returns_error")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 2. state invalid → ERROR
# ============================================================

def test_state_invalid_returns_error():
    d = _tmp_dir()
    try:
        with open(os.path.join(d, "state.json"), "w") as f:
            f.write("{not valid json")
        result = reconcile_public(d, "BTC-SWAP-USDT", "1h")
        assert result["status"] == STATUS_ERROR

        chk = _find_check(result, "state_loads")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "error"
        print("✅ test_state_invalid_returns_error")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 3. hash mismatch → ERROR (check دقیق)
# ============================================================

def test_hash_mismatch_returns_error():
    d = _tmp_dir()
    try:
        klines = _fake_klines(MIN_KLINES)
        state = _valid_state(klines[-1][0])
        state["strategy_hash"] = "sha256:" + "0" * 64
        save_state(d, state)

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        assert result["status"] == STATUS_ERROR

        chk = _find_check(result, "hash_match")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "error"
        print("✅ test_hash_mismatch_returns_error")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 4. hash valid — صراحت در assert
# ============================================================

def test_hash_valid_explicit():
    """
    صراحت: مطمئن شو state["strategy_hash"] دقیقاً
    compute_strategy_hash() است.
    """
    state = _valid_state(BASE_TS)
    assert state["strategy_hash"] == compute_strategy_hash(), (
        f"state hash not aligned with project hash. "
        f"state={state['strategy_hash']} expected={compute_strategy_hash()}"
    )
    print("✅ test_hash_valid_explicit")


# ============================================================
# 5. valid state → OK
# ============================================================

def test_valid_state_returns_ok():
    d = _tmp_dir()
    try:
        # gap=0: lpct = last_candle_ts
        klines = _fake_klines(MIN_KLINES)
        lpct = klines[-1][0]
        state = _valid_state(lpct)

        # صراحت: hash درست است
        assert state["strategy_hash"] == compute_strategy_hash()

        save_state(d, state)

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        failed = _all_failed_checks(result)
        assert failed == [], f"unexpected failed checks: {failed}"
        assert result["status"] == STATUS_OK
        print("✅ test_valid_state_returns_ok")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 6. last_processed = None → WARNING (check دقیق)
# ============================================================

def test_last_processed_none_returns_warning():
    d = _tmp_dir()
    try:
        klines = _fake_klines(MIN_KLINES)
        state = _valid_state(klines[-1][0])
        state["last_processed_candle_ts"] = None
        save_state(d, state)

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        assert result["status"] == STATUS_WARNING

        chk = _find_check(result, "last_processed_vs_latest")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "warning"

        # هیچ خطای واقعی نباید باشد
        errs = _errors(result)
        assert errs == [], f"unexpected errors: {errs}"
        print("✅ test_last_processed_none_returns_warning")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 7. last_processed آینده → ERROR (نسبی)
# ============================================================

def test_last_processed_future_returns_error():
    d = _tmp_dir()
    try:
        klines = _fake_klines(MIN_KLINES)
        # مقدار نسبی، بدون hardcode
        future_lpct = klines[-1][0] + 1000 * INTERVAL_MS
        save_state(d, _valid_state(future_lpct))

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        assert result["status"] == STATUS_ERROR

        chk = _find_check(result, "last_processed_vs_latest")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "error"
        print("✅ test_last_processed_future_returns_error")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 8. klines ناکافی → WARNING فقط (check دقیق)
# ============================================================

def test_insufficient_klines_triggers_warning_only():
    d = _tmp_dir()
    try:
        # MIN_KLINES - 1 → دقیقاً یکی کمتر از آستانه
        n = MIN_KLINES - 1
        klines = _fake_klines(n)
        save_state(d, _valid_state(klines[-1][0]))

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        assert result["status"] == STATUS_WARNING

        chk = _find_check(result, "klines_sufficient")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "warning"

        errs = _errors(result)
        assert errs == [], f"unexpected errors: {errs}"
        print("✅ test_insufficient_klines_triggers_warning_only")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 9. gap دقیقاً 2 کندل → WARNING
# ============================================================

def test_gap_of_2_triggers_warning():
    """
    قرارداد واقعی reconciler:
      missing = (delta // interval_ms) - 1
      if missing >= GAP_WARNING_THRESHOLD(2): WARNING

    طراحی:
      lpct = T0
      last_candle_ts = T0 + 3*interval
      delta = 3*interval
      missing = 3 - 1 = 2  →  WARNING
    """
    d = _tmp_dir()
    try:
        lpct = BASE_TS
        last_candle_ts = lpct + 3 * INTERVAL_MS
        first_ts = last_candle_ts - (MIN_KLINES - 1) * INTERVAL_MS
        klines = _fake_klines(MIN_KLINES, start_ts=first_ts)

        # sanity
        assert klines[-1][0] == last_candle_ts, (
            f"setup error: last={klines[-1][0]} expected={last_candle_ts}"
        )

        save_state(d, _valid_state(lpct))

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        assert result["status"] == STATUS_WARNING

        chk = _find_check(result, "no_missing_candles")
        assert chk is not None
        assert chk["ok"] is False
        assert chk["level"] == "warning"

        errs = _errors(result)
        assert errs == [], f"unexpected errors: {errs}"
        print("✅ test_gap_of_2_triggers_warning")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 10. gap دقیقاً 1 کندل → OK (boundary)
# ============================================================

def test_gap_of_1_does_not_trigger_warning():
    """
    lpct = T0
    last_candle_ts = T0 + 2*interval
    delta = 2*interval
    missing = 2 - 1 = 1  < GAP_WARNING_THRESHOLD(2)  →  OK
    """
    d = _tmp_dir()
    try:
        lpct = BASE_TS
        last_candle_ts = lpct + 2 * INTERVAL_MS
        first_ts = last_candle_ts - (MIN_KLINES - 1) * INTERVAL_MS
        klines = _fake_klines(MIN_KLINES, start_ts=first_ts)

        save_state(d, _valid_state(lpct))

        with patch("reconciler.fetch_klines", return_value=klines):
            result = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        failed = _all_failed_checks(result)
        assert failed == [], (
            f"unexpected failed checks at gap=1: {failed}"
        )
        assert result["status"] == STATUS_OK
        print("✅ test_gap_of_1_does_not_trigger_warning")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 11. Read-only guarantee
# ============================================================

def test_read_only_guarantee():
    """reconciler نباید state.json یا events.jsonl را تغییر دهد."""
    d = _tmp_dir()
    try:
        klines = _fake_klines(MIN_KLINES)
        save_state(d, _valid_state(klines[-1][0]))

        state_path = os.path.join(d, "state.json")
        events_path = os.path.join(d, "events.jsonl")

        with open(state_path, "rb") as f:
            state_before = f.read()

        with open(events_path, "w") as f:
            f.write('{"event_id":"x","seq":1}\n')
        with open(events_path, "rb") as f:
            events_before = f.read()

        with patch("reconciler.fetch_klines", return_value=klines):
            _ = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        with open(state_path, "rb") as f:
            state_after = f.read()
        with open(events_path, "rb") as f:
            events_after = f.read()

        assert state_before == state_after, "state.json تغییر کرد"
        assert events_before == events_after, "events.jsonl تغییر کرد"
        print("✅ test_read_only_guarantee")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 12. Deterministic — کل output
# ============================================================

def test_deterministic_full_output():
    """
    همان ورودی → همان خروجی کامل.
    چون reconciler از time.time() استفاده نمی‌کند و
    now_ms نسبی از klines محاسبه می‌شود، detail strings
    هم باید یکسان باشند.
    """
    d = _tmp_dir()
    try:
        klines = _fake_klines(MIN_KLINES)
        save_state(d, _valid_state(klines[-1][0]))

        with patch("reconciler.fetch_klines", return_value=klines):
            r1 = reconcile_public(d, "BTC-SWAP-USDT", "1h")
            r2 = reconcile_public(d, "BTC-SWAP-USDT", "1h")

        # کل output باید یکسان باشد
        assert r1 == r2, (
            f"outputs differ:\n r1={r1}\n r2={r2}"
        )
        print("✅ test_deterministic_full_output")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_state_missing_returns_error,
        test_state_invalid_returns_error,
        test_hash_mismatch_returns_error,
        test_hash_valid_explicit,
        test_valid_state_returns_ok,
        test_last_processed_none_returns_warning,
        test_last_processed_future_returns_error,
        test_insufficient_klines_triggers_warning_only,
        test_gap_of_2_triggers_warning,
        test_gap_of_1_does_not_trigger_warning,
        test_read_only_guarantee,
        test_deterministic_full_output,
    ]
    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Reconciler Unit Tests")
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