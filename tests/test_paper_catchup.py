"""
test_paper_catchup.py — تست رفتار catch-up در Paper Trading.

سیاست (طبق طراحی تأییدشده):
  - تمام کندل‌های جدید (ts > last_processed_candle_ts) به ترتیب صعودی
    پردازش می‌شوند.
  - سقف ۱۰۰ کندل در هر اجرا (self-healing: باقی برای اجرای بعد).
  - state پس از هر کندل موفق ذخیره می‌شود.
  - خطا در میانه، آخرین state موفق را حفظ می‌کند.
  - وضعیت lag (اگر وجود داشته باشد) در state و stdout دیده می‌شود.

⚠️ نسخه‌ی فعلی run_paper فقط آخرین کندل را پردازش می‌کند.
   بنابراین اکثر تست‌های این فایل شکست می‌خورند — که هدف است.
"""

import io
import json
import os
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import run_paper as rp
from state_manager import default_state


BASE_TS = 1_698_796_800_000   # 2023-11-01T00:00:00Z (aligned 1h)
HOUR = 3_600_000

FAKE_SPECS = {
    "symbol": "BTC-SWAP-USDT",
    "contract_multiplier": 0.001,
    "min_qty": 1.0,
    "max_qty": 1_000_000.0,
    "qty_step": 1.0,
    "min_notional": 5.0,
    "max_leverage": 100.0,
}


# ============================================================
# Fixtures
# ============================================================

def _make_candles(n, start_ts=BASE_TS):
    result = []
    for i in range(n):
        ts = start_ts + i * HOUR
        result.append([
            ts,
            "100.0", "100.5", "99.5", "100.0", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0",
        ])
    return result


def _setup_state(td, initial_last_processed):
    state_dir = os.path.join(td, "state")
    os.makedirs(state_dir, exist_ok=True)
    state = default_state(initial_equity=1000.0)
    state["last_processed_candle_ts"] = initial_last_processed
    path = os.path.join(state_dir, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return state_dir


def _read_state(state_dir):
    path = os.path.join(state_dir, "state.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_process_spy(fail_on_call=None):
    """spy روی rp.process_candle. تعداد فراخوانی‌ها و indexها را ثبت می‌کند."""
    original = rp.process_candle
    calls = []

    def spy(state, klines, indicators, idx, specs, config, signal=None):
        calls.append(idx)
        if fail_on_call is not None and len(calls) == fail_on_call:
            raise RuntimeError(f"injected failure on call {fail_on_call}")
        return original(
            state, klines, indicators, idx, specs, config, signal=signal,
        )

    spy.calls = calls
    return spy


def _make_save_spy():
    """spy روی rp.save_state. مقادیر last_processed_candle_ts را ثبت می‌کند."""
    original = rp.save_state
    calls = []

    def spy(state_dir, state):
        calls.append(state.get("last_processed_candle_ts"))
        return original(state_dir, state)

    spy.calls = calls
    return spy


def _run_main(state_dir, klines, process_spy=None, save_spy=None):
    env = {
        "STATE_DIR": state_dir,
        "SYMBOL": "BTC-SWAP-USDT",
        "INTERVAL": "1h",
    }
    patches = [
        patch.dict(os.environ, env),
        patch.object(rp, "get_symbol_specs", return_value=FAKE_SPECS),
        patch.object(rp, "fetch_closed_klines", return_value=klines),
        patch.object(rp, "is_kill_switch_active", return_value=(False, None)),
    ]
    if process_spy is not None:
        patches.append(patch.object(rp, "process_candle", new=process_spy))
    if save_spy is not None:
        patches.append(patch.object(rp, "save_state", new=save_spy))

    buf = io.StringIO()
    exit_code = None
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with redirect_stdout(buf):
            try:
                rp.main()
            except SystemExit as e:
                exit_code = e.code
    return exit_code, buf.getvalue()


# ============================================================
# Tests
# ============================================================

def test_no_new_candle_is_noop():
    spy = _make_process_spy()
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[-1][0])
        state_dir = _setup_state(td, initial_ts)
        exit_code, _ = _run_main(state_dir, klines, process_spy=spy)

        assert spy.calls == [], (
            f"no-op expected but process_candle called at {spy.calls}"
        )
        assert _read_state(state_dir)["last_processed_candle_ts"] == initial_ts


def test_one_new_candle_processed():
    spy = _make_process_spy()
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[298][0])
        state_dir = _setup_state(td, initial_ts)
        _run_main(state_dir, klines, process_spy=spy)

        assert spy.calls == [299], f"expected [299], got {spy.calls}"
        assert _read_state(state_dir)["last_processed_candle_ts"] == \
            int(klines[299][0])


def test_five_new_candles_processed_in_order():
    spy = _make_process_spy()
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[294][0])
        state_dir = _setup_state(td, initial_ts)
        _run_main(state_dir, klines, process_spy=spy)

        assert spy.calls == [295, 296, 297, 298, 299], (
            f"expected sequential [295..299], got {spy.calls}"
        )
        assert _read_state(state_dir)["last_processed_candle_ts"] == \
            int(klines[299][0])


def test_exactly_100_candles_allowed():
    spy = _make_process_spy()
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[199][0])
        state_dir = _setup_state(td, initial_ts)
        exit_code, _ = _run_main(state_dir, klines, process_spy=spy)

        assert spy.calls == list(range(200, 300)), (
            f"expected [200..299], got {spy.calls}"
        )
        assert exit_code in (0, None)
        # lag صفر باید ثبت شود (اگر implementation فیلد دارد)
        final = _read_state(state_dir)
        if "catchup_lag_candles" in final:
            assert final["catchup_lag_candles"] == 0


def test_over_100_processes_oldest_100():
    """۱۰۱ کندل جدید → ۱۰۰ قدیمی پردازش، ۱ باقی برای اجرای بعد."""
    spy = _make_process_spy()
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[198][0])   # 199..299 = 101 new
        state_dir = _setup_state(td, initial_ts)
        exit_code, stdout = _run_main(state_dir, klines, process_spy=spy)

        assert spy.calls == list(range(199, 299)), (
            f"expected oldest 100 [199..298], got {spy.calls}"
        )
        saved = _read_state(state_dir)["last_processed_candle_ts"]
        assert saved == int(klines[298][0]), (
            f"state should advance to 298 (last processed), got {saved}"
        )
        # exit code 0 چون پیشرفت کرده
        assert exit_code in (0, None), (
            f"self-healing should exit 0, got {exit_code}"
        )


def test_lag_visible_after_partial_catchup():
    """پس از پردازش ۱۰۰ کندل از ۱۰۱، lag باید دیده شود."""
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[198][0])
        state_dir = _setup_state(td, initial_ts)
        exit_code, stdout = _run_main(state_dir, klines)

        final = _read_state(state_dir)
        lag_in_state = final.get("catchup_lag_candles")
        lag_in_stdout = (
            "lag" in stdout.lower()
            or "pending" in stdout.lower()
            or "remaining" in stdout.lower()
        )

        assert lag_in_state is not None or lag_in_stdout, (
            f"lag not visible. state keys: {sorted(final.keys())}, "
            f"stdout tail: {stdout[-500:]!r}"
        )
        if lag_in_state is not None:
            assert lag_in_state == 1, (
                f"expected lag=1, got {lag_in_state}"
            )


def test_next_run_processes_remaining():
    """اجرای دوم، کندل باقی‌مانده‌ی بعد از ۱۰۰تایی اول را پردازش می‌کند."""
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[198][0])
        state_dir = _setup_state(td, initial_ts)

        # اجرای اول: ۱۰۰ کندل [199..298]
        spy1 = _make_process_spy()
        _run_main(state_dir, klines, process_spy=spy1)
        assert spy1.calls == list(range(199, 299)), (
            f"run1 expected [199..298], got {spy1.calls}"
        )

        # اجرای دوم: فقط 299 باقی‌مانده
        spy2 = _make_process_spy()
        _run_main(state_dir, klines, process_spy=spy2)
        assert spy2.calls == [299], (
            f"run2 expected [299], got {spy2.calls}"
        )
        assert _read_state(state_dir)["last_processed_candle_ts"] == \
            int(klines[299][0])


def test_failure_on_3rd_candle_preserves_2_successful():
    """خطا در ۳اُمین پردازش → فقط ۲ کندل اول ذخیره می‌شوند."""
    spy = _make_process_spy(fail_on_call=3)
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[294][0])   # 295..299 new
        state_dir = _setup_state(td, initial_ts)
        exit_code, _ = _run_main(state_dir, klines, process_spy=spy)

        # ۳ فراخوانی تلاش شد
        assert spy.calls == [295, 296, 297], (
            f"expected [295, 296, 297] attempts, got {spy.calls}"
        )
        # فقط ۲ کندل اول موفق بودند و ذخیره شدند
        saved = _read_state(state_dir)["last_processed_candle_ts"]
        assert saved == int(klines[296][0]), (
            f"state should be at 296 (2nd success), got {saved}"
        )
        assert exit_code == 1, (
            f"failure should produce exit 1, got {exit_code}"
        )


def test_state_saved_after_each_candle_with_correct_sequence():
    """state پس از هر کندل با ts صحیح ذخیره می‌شود."""
    spy = _make_process_spy()
    save_spy = _make_save_spy()
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        initial_ts = int(klines[294][0])   # 295..299 new
        state_dir = _setup_state(td, initial_ts)
        _run_main(
            state_dir, klines, process_spy=spy, save_spy=save_spy,
        )

        expected = [
            int(klines[295][0]),
            int(klines[296][0]),
            int(klines[297][0]),
            int(klines[298][0]),
            int(klines[299][0]),
        ]
        # نادیده گرفتن ذخیره‌های قبل از اولین کندل جدید (اگر وجود دارند)
        actual_new_candle_saves = [
            ts for ts in save_spy.calls
            if ts is not None and ts > initial_ts
        ]
        assert actual_new_candle_saves == expected, (
            f"saved ts sequence mismatch.\n"
            f"  expected: {expected}\n"
            f"  actual  : {actual_new_candle_saves}\n"
            f"  all     : {save_spy.calls}"
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_no_new_candle_is_noop,
        test_one_new_candle_processed,
        test_five_new_candles_processed_in_order,
        test_exactly_100_candles_allowed,
        test_over_100_processes_oldest_100,
        test_lag_visible_after_partial_catchup,
        test_next_run_processes_remaining,
        test_failure_on_3rd_candle_preserves_2_successful,
        test_state_saved_after_each_candle_with_correct_sequence,
    ]

    passed = 0
    failed = 0
    failures = []

    print("=" * 68)
    print(f"🧪 Paper Catch-up Tests ({len(tests)} total)")
    print("=" * 68)
    print()

    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"❌ {t.__name__}: {exc}")
            failures.append((t.__name__, str(exc)))
            failed += 1
        except Exception as exc:
            print(f"❌ {t.__name__}: {type(exc).__name__}: {exc}")
            failures.append((t.__name__, f"{type(exc).__name__}: {exc}"))
            failed += 1

    print()
    print("=" * 68)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 68)

    if failures:
        print()
        print("⚠️  تحلیل شکست‌ها:")
        print()
        for name, msg in failures:
            print(f"  • {name}")
            print(f"    {msg}")
            print()

    if failed:
        sys.exit(1)