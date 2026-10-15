"""
test_reconcile_with_data.py — تست تابع pure reconciler.

قرارداد مورد انتظار:
  reconcile_with_data(state, klines, interval, now_ms) → dict
    - now_ms اجباری است (deterministic)
    - pure: no HTTP, no disk I/O
    - checks: hash_match, klines_sufficient, last_candle_closed,
              last_processed_vs_latest, no_missing_candles
    - خروجی {"status", "checks", "summary"}

آستانه‌ها (مطابق reconciler.py فعلی):
  - MIN_KLINES = 250
  - GAP_WARNING_THRESHOLD = 2  (یعنی missing >= 2 → WARNING)
  - missing = (last_candle_ts - lpct) // interval_ms - 1

⚠️ این تست‌ها پیش از پیاده‌سازی شکست می‌خورند.
"""

import copy
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from state_manager import default_state


BASE_TS = 1_698_796_800_000
HOUR = 3_600_000


def _make_candles(n, start_ts=BASE_TS):
    return [
        [start_ts + i * HOUR,
         "100.0", "100.5", "99.5", "100.0", "1000.0",
         start_ts + i * HOUR + HOUR - 1, "0", 0, "0", "0", "0"]
        for i in range(n)
    ]


def _make_state(last_processed_ts=None, hash_override=None):
    s = default_state(initial_equity=1000.0)
    s["last_processed_candle_ts"] = last_processed_ts
    if hash_override is not None:
        s["strategy_hash"] = hash_override
    return s


def _get_check(result, name):
    return next((c for c in result["checks"] if c["name"] == name), None)


# ============================================================
# 1. Import / existence
# ============================================================

def test_function_exists():
    from reconciler import reconcile_with_data  # noqa: F401
    assert callable(reconcile_with_data)


def test_now_ms_is_required_parameter():
    """now_ms باید پارامتر اجباری باشد (نه پیش‌فرض None)."""
    import inspect
    from reconciler import reconcile_with_data
    sig = inspect.signature(reconcile_with_data)
    params = list(sig.parameters.keys())
    assert "now_ms" in params, "now_ms must be a parameter"
    p = sig.parameters["now_ms"]
    assert p.default is inspect.Parameter.empty, (
        "now_ms must NOT have a default (must be explicit)"
    )


# ============================================================
# 2. Happy path
# ============================================================

def test_valid_state_and_klines_returns_ok():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    now_ms = last_ts + 2 * HOUR
    result = reconcile_with_data(state, klines, "1h", now_ms)
    assert result["status"] == "OK", (
        f"expected OK, got {result['status']}: {result['summary']}"
    )
    assert all(
        c["ok"] for c in result["checks"] if c["level"] == "error"
    )


def test_hash_match_ok():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    hm = _get_check(result, "hash_match")
    assert hm is not None and hm["ok"] is True


# ============================================================
# 3. Hash mismatch
# ============================================================

def test_hash_mismatch_is_error():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(
        last_processed_ts=last_ts,
        hash_override="sha256:" + "0" * 64,
    )
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    assert result["status"] == "ERROR"
    hm = _get_check(result, "hash_match")
    assert hm["ok"] is False
    assert hm["level"] == "error"


def test_missing_hash_is_error():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    state["strategy_hash"] = None
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    assert result["status"] == "ERROR"
    hm = _get_check(result, "hash_match")
    assert hm["ok"] is False


# ============================================================
# 4. Klines count
# ============================================================

def test_insufficient_klines_is_warning_not_error():
    """۲۰۰ کندل < 250 → WARNING، نه ERROR."""
    from reconciler import reconcile_with_data
    klines = _make_candles(200)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    ks = _get_check(result, "klines_sufficient")
    assert ks["ok"] is False
    assert ks["level"] == "warning"


def test_exactly_min_klines_is_ok():
    """۲۵۰ کندل = MIN_KLINES → OK."""
    from reconciler import reconcile_with_data
    klines = _make_candles(250)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    ks = _get_check(result, "klines_sufficient")
    assert ks["ok"] is True


def test_zero_klines_no_crash():
    """klines خالی → نباید exception؛ باید نتایج معتبر برگرداند."""
    from reconciler import reconcile_with_data
    state = _make_state(last_processed_ts=None)
    result = reconcile_with_data(state, [], "1h", BASE_TS)
    ks = _get_check(result, "klines_sufficient")
    assert ks is not None and ks["ok"] is False
    assert ks["level"] == "warning"
    assert result["status"] in ("WARNING", "ERROR")


# ============================================================
# 5. last_processed_candle_ts
# ============================================================

def test_last_processed_none_is_warning():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=None)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    lp = _get_check(result, "last_processed_vs_latest")
    assert lp["ok"] is False
    assert lp["level"] == "warning"


def test_last_processed_in_future_is_error():
    """
    lpct باید در آینده‌ی now_ms باشد تا خطا فعال شود.

    now_ms = last_ts + 5h
    lpct   = last_ts + 10h  → در آینده، هم نسبت به last_ts و هم now_ms
    """
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    lpct = last_ts + 10 * HOUR
    now_ms = last_ts + 5 * HOUR
    state = _make_state(last_processed_ts=lpct)
    result = reconcile_with_data(state, klines, "1h", now_ms)
    assert result["status"] == "ERROR", (
        f"lpct({lpct}) > last({last_ts}) باید ERROR بدهد، "
        f"got {result['status']}"
    )
    lp = _get_check(result, "last_processed_vs_latest")
    assert lp["ok"] is False
    assert lp["level"] == "error"


def test_last_processed_equal_latest_is_ok():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    lp = _get_check(result, "last_processed_vs_latest")
    assert lp["ok"] is True


# ============================================================
# 6. Gap detection
# ============================================================
# محاسبه‌ی فعلی reconciler:
#   delta = last_candle_ts - lpct
#   missing = (delta // interval_ms) - 1
#   if missing < 0: missing = 0
#   if missing >= GAP_WARNING_THRESHOLD (2): WARNING
# نتیجه:
#   lpct = last         → missing = 0  → OK
#   lpct = last - 1h    → missing = 0  → OK
#   lpct = last - 2h    → missing = 1  → OK
#   lpct = last - 3h    → missing = 2  → WARNING
# ============================================================

def test_gap_of_zero_candles_is_ok():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    mc = _get_check(result, "no_missing_candles")
    assert mc["ok"] is True


def test_gap_of_one_candle_is_ok():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    lpct = last_ts - 1 * HOUR
    state = _make_state(last_processed_ts=lpct)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    mc = _get_check(result, "no_missing_candles")
    assert mc["ok"] is True, (
        f"gap of 1 (missing=0) باید OK باشد، got: {mc['detail']}"
    )


def test_gap_of_two_candles_is_still_ok():
    """
    last_processed = last - 2h → delta = 2h → missing = 1
    آستانه ۲ است، پس هنوز OK.
    """
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    lpct = last_ts - 2 * HOUR
    state = _make_state(last_processed_ts=lpct)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    mc = _get_check(result, "no_missing_candles")
    assert mc["ok"] is True, (
        f"missing=1 زیر آستانه ۲ → OK، got: {mc['detail']}"
    )


def test_gap_of_three_candles_is_warning():
    """
    last_processed = last - 3h → delta = 3h → missing = 2
    آستانه ۲ → WARNING.
    """
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    lpct = last_ts - 3 * HOUR
    state = _make_state(last_processed_ts=lpct)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    assert result["status"] == "WARNING"
    mc = _get_check(result, "no_missing_candles")
    assert mc["ok"] is False
    assert mc["level"] == "warning"


# ============================================================
# 7. Last candle closed
# ============================================================

def test_last_candle_closed_ok():
    """now_ms = close_time → بسته است."""
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    # now = last + interval (دقیقاً مرز بسته‌شدن)
    result = reconcile_with_data(state, klines, "1h", last_ts + HOUR)
    lc = _get_check(result, "last_candle_closed")
    assert lc["ok"] is True


def test_last_candle_open_is_error():
    """now_ms < close_time → باز است."""
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    # now < last + interval
    result = reconcile_with_data(state, klines, "1h", last_ts)
    lc = _get_check(result, "last_candle_closed")
    assert lc["ok"] is False
    assert result["status"] == "ERROR"


# ============================================================
# 8. Structure / contract
# ============================================================

def test_result_has_required_keys():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    assert set(result.keys()) == {"status", "checks", "summary"}
    assert result["status"] in ("OK", "WARNING", "ERROR")
    assert isinstance(result["checks"], list)
    assert isinstance(result["summary"], str)


def test_expected_check_names_present():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    result = reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    names = {c["name"] for c in result["checks"]}
    expected = {
        "hash_match",
        "klines_sufficient",
        "last_candle_closed",
        "last_processed_vs_latest",
        "no_missing_candles",
    }
    missing = expected - names
    assert not missing, f"missing checks: {missing}"


# ============================================================
# 9. Purity
# ============================================================

def test_does_not_mutate_state():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    state_before = copy.deepcopy(state)
    reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    assert state == state_before, "reconcile_with_data state را تغییر داد"


def test_does_not_mutate_klines():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    klines_before = copy.deepcopy(klines)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    reconcile_with_data(state, klines, "1h", last_ts + 2 * HOUR)
    assert klines == klines_before, "reconcile_with_data klines را تغییر داد"


def test_deterministic_same_input():
    from reconciler import reconcile_with_data
    klines = _make_candles(300)
    last_ts = int(klines[-1][0])
    state = _make_state(last_processed_ts=last_ts)
    now_ms = last_ts + 2 * HOUR
    r1 = reconcile_with_data(state, klines, "1h", now_ms)
    r2 = reconcile_with_data(state, klines, "1h", now_ms)
    assert r1 == r2


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # 1. Existence (2)
        test_function_exists,
        test_now_ms_is_required_parameter,
        # 2. Happy path (2)
        test_valid_state_and_klines_returns_ok,
        test_hash_match_ok,
        # 3. Hash mismatch (2)
        test_hash_mismatch_is_error,
        test_missing_hash_is_error,
        # 4. Klines count (3)
        test_insufficient_klines_is_warning_not_error,
        test_exactly_min_klines_is_ok,
        test_zero_klines_no_crash,
        # 5. last_processed (3)
        test_last_processed_none_is_warning,
        test_last_processed_in_future_is_error,
        test_last_processed_equal_latest_is_ok,
        # 6. Gap (4)
        test_gap_of_zero_candles_is_ok,
        test_gap_of_one_candle_is_ok,
        test_gap_of_two_candles_is_still_ok,
        test_gap_of_three_candles_is_warning,
        # 7. Last candle closed (2)
        test_last_candle_closed_ok,
        test_last_candle_open_is_error,
        # 8. Structure (2)
        test_result_has_required_keys,
        test_expected_check_names_present,
        # 9. Purity (3)
        test_does_not_mutate_state,
        test_does_not_mutate_klines,
        test_deterministic_same_input,
    ]

    passed, failed = 0, 0
    failures = []
    print("=" * 68)
    print(f"🧪 Reconcile-with-data Tests ({len(tests)} total)")
    print("=" * 68)
    print()
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"❌ {t.__name__}: {type(exc).__name__}: {exc}")
            failures.append((t.__name__, str(exc)))
            failed += 1
    print()
    print("=" * 68)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 68)
    if failures:
        print()
        print("⚠️  شکست‌ها:")
        print()
        for name, msg in failures:
            print(f"  • {name}")
            print(f"    {msg}")
            print()
    if failed:
        sys.exit(1)