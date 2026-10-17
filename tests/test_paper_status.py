"""
test_paper_status.py — تست نوشتن paper_status.json.

قرارداد:
  - run_paper در finally یک فایل paper_status.json می‌نویسد
  - نوشتن اتمیک: tempfile در همان پوشه + os.replace
  - اگر نوشتن شکست خورد → exit code 1
  - در هر خروجی (success / no_op / insufficient_data / aborted /
    failed) نوشته می‌شود
  - reconciler_status ممکن است null باشد اگر reconciler اجرا نشد

run_status مقادیر مجاز:
  success           — اجرا کامل، همه‌چیز OK
  no_op             — کندل جدید نیست، داده معتبر
  insufficient_data — کندل کافی نیست
  aborted           — hash mismatch یا kill switch یا مشابه
  failed            — خطای فنی

v2.3 (سازگاری با main()->int):
  _run_main ابتدا خروجی main() را به‌عنوان int می‌گیرد؛
  اگر SystemExit رخ داد (سازگاری عقب‌رو)، code را از آن می‌گیرد.
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


BASE_TS = 1_698_796_800_000
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

WRONG_HASH = "sha256:" + "0" * 64

REQUIRED_FIELDS = [
    "schema_version",
    "run_started_at_iso",
    "run_finished_at_iso",
    "run_status",
    "run_exit_code",
    "strategy_hash",
    "reconciler_status",
    "reconciler_summary",
    "last_processed_candle_ts",
    "candles_processed_this_run",
    "catchup_lag",
    "total_new_detected",
    "equity",
    "peak_equity",
    "has_position",
    "error_message",
]

ALLOWED_RUN_STATUS = {
    "success", "no_op", "insufficient_data", "aborted", "failed",
}


def _make_candles(n, start_ts=BASE_TS):
    return [
        [start_ts + i * HOUR,
         "100.0", "100.5", "99.5", "100.0", "1000.0",
         start_ts + i * HOUR + HOUR - 1, "0", 0, "0", "0", "0"]
        for i in range(n)
    ]


def _setup_state_dir(td, hash_override=None, last_processed=None):
    state_dir = os.path.join(td, "state")
    os.makedirs(state_dir, exist_ok=True)
    state = default_state(initial_equity=1000.0)
    state["symbol"] = "BTC-SWAP-USDT"
    state["last_processed_candle_ts"] = last_processed
    if hash_override is not None:
        state["strategy_hash"] = hash_override
    with open(os.path.join(state_dir, "state.json"), "w") as f:
        json.dump(state, f)
    return state_dir


def _run_main(state_dir, klines):
    """
    اجرای rp.main() با mockهای لازم.

    v2.3: main() حالا int برمی‌گرداند (نه SystemExit).
    برای سازگاری عقب‌رو، SystemExit را هم می‌گیریم.
    """
    env = {
        "STATE_DIR": state_dir,
        "SYMBOL": "BTC-SWAP-USDT",
        "INTERVAL": "1h",
    }
    buf = io.StringIO()
    exit_code = None
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch.object(rp, "get_symbol_specs",
                                         return_value=FAKE_SPECS))
        stack.enter_context(patch.object(rp, "fetch_closed_klines",
                                         return_value=klines))
        stack.enter_context(patch.object(rp, "is_kill_switch_active",
                                         return_value=(False, None)))
        with redirect_stdout(buf):
            try:
                result = rp.main()
                exit_code = result if isinstance(result, int) else None
            except SystemExit as e:
                exit_code = e.code
    return exit_code, buf.getvalue()


def _read_status(state_dir):
    path = os.path.join(state_dir, "paper_status.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


# ============================================================
# 1. Existence
# ============================================================

def test_write_function_exists():
    from run_paper import _write_paper_status_atomic  # noqa: F401
    assert callable(_write_paper_status_atomic)


# ============================================================
# 2. Atomic write — low level
# ============================================================

def test_write_creates_file_with_valid_json():
    from run_paper import _write_paper_status_atomic
    with tempfile.TemporaryDirectory() as td:
        status = {"schema_version": "1.0.0", "run_status": "success"}
        _write_paper_status_atomic(td, status)
        path = os.path.join(td, "paper_status.json")
        assert os.path.exists(path)
        with open(path) as f:
            assert json.load(f) == status


def test_write_creates_parent_dir():
    from run_paper import _write_paper_status_atomic
    with tempfile.TemporaryDirectory() as td:
        nested = os.path.join(td, "a", "b")
        _write_paper_status_atomic(nested, {"x": 1})
        assert os.path.exists(os.path.join(nested, "paper_status.json"))


def test_write_does_not_leave_tempfiles_on_success():
    from run_paper import _write_paper_status_atomic
    with tempfile.TemporaryDirectory() as td:
        _write_paper_status_atomic(td, {"x": 1})
        leftovers = [x for x in os.listdir(td) if x != "paper_status.json"]
        assert not leftovers, f"tempfiles باقی ماندند: {leftovers}"


def test_write_is_atomic_on_replace_failure():
    from run_paper import _write_paper_status_atomic
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "paper_status.json")
        with open(target, "w") as f:
            json.dump({"old": True}, f)

        with patch.object(rp.os, "replace",
                          side_effect=OSError("disk full")):
            try:
                _write_paper_status_atomic(td, {"new": True})
                assert False, "should have raised OSError"
            except OSError:
                pass

        with open(target) as f:
            assert json.load(f) == {"old": True}, "فایل قدیمی تغییر کرد"

        leftovers = [x for x in os.listdir(td) if x != "paper_status.json"]
        assert not leftovers, f"tempfiles باقی ماندند: {leftovers}"


# ============================================================
# 3. Integration — main writes status
# ============================================================

def test_successful_run_writes_status():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, last_processed=int(klines[298][0]),
        )
        exit_code, _ = _run_main(state_dir, klines)
        assert exit_code in (0, None)
        status = _read_status(state_dir)
        assert status is not None, "paper_status.json نوشته نشد"
        assert status["run_status"] == "success"
        assert status["run_exit_code"] == 0
        assert status["candles_processed_this_run"] == 1


def test_hash_mismatch_writes_aborted_status():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, hash_override=WRONG_HASH,
            last_processed=int(klines[298][0]),
        )
        exit_code, _ = _run_main(state_dir, klines)
        assert exit_code == 1
        status = _read_status(state_dir)
        assert status is not None, (
            "paper_status.json در hash mismatch نوشته نشد"
        )
        assert status["run_status"] == "aborted"
        assert status["run_exit_code"] == 1
        assert status["error_message"] is not None
        assert "hash" in status["error_message"].lower() or \
               "strategy" in status["error_message"].lower()


def test_no_new_candle_writes_no_op_status():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, last_processed=int(klines[-1][0]),
        )
        _run_main(state_dir, klines)
        status = _read_status(state_dir)
        assert status is not None
        assert status["run_status"] == "no_op"


def test_insufficient_klines_writes_insufficient_data():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(200)  # کمتر از ۲۵۰
        state_dir = _setup_state_dir(td, last_processed=None)
        _run_main(state_dir, klines)
        status = _read_status(state_dir)
        assert status is not None
        assert status["run_status"] == "insufficient_data"


def test_reconciler_status_can_be_null_on_early_exit():
    """
    در hash mismatch، reconciler اجرا نمی‌شود، پس status باید
    null باشد.
    """
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, hash_override=WRONG_HASH,
            last_processed=int(klines[298][0]),
        )
        _run_main(state_dir, klines)
        status = _read_status(state_dir)
        assert status is not None
        rs = status["reconciler_status"]
        assert rs is None or rs == "not_run", (
            f"expected null or 'not_run', got {rs!r}"
        )


def test_reconciler_status_present_on_success():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, last_processed=int(klines[298][0]),
        )
        _run_main(state_dir, klines)
        status = _read_status(state_dir)
        assert status is not None
        assert status["reconciler_status"] in ("OK", "WARNING", "ERROR")


def test_status_contains_all_required_fields():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, last_processed=int(klines[298][0]),
        )
        _run_main(state_dir, klines)
        status = _read_status(state_dir)
        missing = [k for k in REQUIRED_FIELDS if k not in status]
        assert not missing, f"missing fields: {missing}"


def test_run_status_value_is_known():
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, last_processed=int(klines[298][0]),
        )
        _run_main(state_dir, klines)
        status = _read_status(state_dir)
        assert status["run_status"] in ALLOWED_RUN_STATUS


def test_write_failure_does_not_crash_main():
    """
    اگر نوشتن paper_status شکست خورد، main باید exit 1 بدهد
    بدون exception بالا.
    """
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _setup_state_dir(
            td, last_processed=int(klines[298][0]),
        )
        with patch.object(rp, "_write_paper_status_atomic",
                          side_effect=OSError("disk full"),
                          create=True):
            exit_code, _ = _run_main(state_dir, klines)
        assert exit_code == 1, (
            f"نوشتن ناموفق باید exit code 1 بدهد، got {exit_code}"
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # 1. Existence (1)
        test_write_function_exists,
        # 2. Atomic write (4)
        test_write_creates_file_with_valid_json,
        test_write_creates_parent_dir,
        test_write_does_not_leave_tempfiles_on_success,
        test_write_is_atomic_on_replace_failure,
        # 3. Integration (9)
        test_successful_run_writes_status,
        test_hash_mismatch_writes_aborted_status,
        test_no_new_candle_writes_no_op_status,
        test_insufficient_klines_writes_insufficient_data,
        test_reconciler_status_can_be_null_on_early_exit,
        test_reconciler_status_present_on_success,
        test_status_contains_all_required_fields,
        test_run_status_value_is_known,
        test_write_failure_does_not_crash_main,
    ]

    passed, failed = 0, 0
    failures = []
    print("=" * 68)
    print(f"🧪 Paper Status Tests ({len(tests)} total)")
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