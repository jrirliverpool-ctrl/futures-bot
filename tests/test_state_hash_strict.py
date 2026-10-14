"""
test_state_hash_strict.py — تست سیاست strict در بارگذاری state.

قرارداد مورد انتظار (پیش از پیاده‌سازی):
  - paper_state_manager.load_state پیش‌فرض strict=True
  - run_paper هنگام hash mismatch با پیام صریح exit(1) می‌دهد
  - state.json و events.jsonl در rejection دست‌نخورده می‌مانند
  - پوزیشن باز در state، هشدار diag صریح می‌گیرد
  - هنگام hash matching، اجرا عادی ادامه می‌یابد

⚠️ این تست‌ها پیش از پیاده‌سازی شکست می‌خورند — هدف است.
"""

import inspect
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
from paper_state_manager import load_state


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


# ============================================================
# Helpers
# ============================================================

def _make_candles(n, start_ts=BASE_TS):
    return [
        [
            start_ts + i * HOUR,
            "100.0", "100.5", "99.5", "100.0", "1000.0",
            start_ts + i * HOUR + HOUR - 1, "0", 0, "0", "0", "0",
        ]
        for i in range(n)
    ]


def _write_state(state_dir, state):
    path = os.path.join(state_dir, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return path


def _make_state_dir(td, *, hash_override=None, with_position=False,
                    last_processed=None):
    state_dir = os.path.join(td, "state")
    os.makedirs(state_dir, exist_ok=True)
    state = default_state(initial_equity=1000.0)
    state["symbol"] = "BTC-SWAP-USDT"
    state["last_processed_candle_ts"] = last_processed
    if hash_override is not None:
        state["strategy_hash"] = hash_override
    if with_position:
        state["position"] = {
            "side": "LONG", "entry_price": 100.0,
            "entry_candle_ts": BASE_TS, "size": 1.0,
            "remaining_size": 1.0, "tp1_size": 0.0,
            "realized_pnl": 0.0, "initial_sl": 98.0,
            "current_sl": 98.0, "initial_tp1": 104.0,
            "tp1_hit": False, "be_moved": False,
            "trailing_active": False,
            "highest_high_since_entry": 100.0,
            "lowest_low_since_entry": 100.0,
            "candles_since_entry": 0, "atr_at_entry": 1.0,
            "signal_candle_ts": BASE_TS - HOUR,
        }
    _write_state(state_dir, state)
    return state_dir


def _read_bytes(path):
    """
    محتوای فایل را بایت‌به‌بایت برمی‌گرداند.
    اگر فایل وجود نداشته باشد → None.
    این حالت برای تأیید «فایل نباید ایجاد شود» مهم است.
    """
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _run_main(state_dir, klines):
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
                rp.main()
            except SystemExit as e:
                exit_code = e.code
    return exit_code, buf.getvalue()


def _assert_mismatch_hint(stdout):
    """
    تأیید می‌کند که پیام خروجی، علت mismatch را صریح نشان می‌دهد.
    """
    low = stdout.lower()
    has_hash_hint = (
        "hash mismatch" in low
        or "strategy_hash" in low
        or "different strategy" in low
        or "belongs to" in low
    )
    assert has_hash_hint, (
        "خروجی، علت hash mismatch را صریح نشان نمی‌دهد.\n"
        f"stdout tail: {stdout[-800:]!r}"
    )


def _assert_position_hint(stdout):
    """
    تأیید می‌کند که هشدار پوزیشن باز، صریح و مرتبط با mismatch است.
    نه فقط کلمه‌ی position در یک پیام عمومی.
    """
    low = stdout.lower()
    has_position = "position" in low
    has_action = (
        "open position" in low
        or "manual intervention" in low
        or "manual" in low
        or "intervention" in low
        or "do not delete" in low
    )
    assert has_position and has_action, (
        "هشدار پوزیشن باز به‌اندازه‌ی کافی صریح نیست.\n"
        f"  has_position={has_position} has_action={has_action}\n"
        f"stdout tail: {stdout[-800:]!r}"
    )


# ============================================================
# Tests
# ============================================================

def test_default_load_is_strict():
    """پیش‌فرض load_state باید strict=True باشد."""
    sig = inspect.signature(load_state)
    default = sig.parameters["strict"].default
    assert default is True, (
        f"load_state strict default is {default!r}, expected True"
    )


def test_matching_hash_loads_normally():
    """با hash یکسان، بارگذاری بدون خطا."""
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=None)
        loaded = load_state(state_dir)
        assert loaded["strategy_hash"] == default_state()["strategy_hash"]


def test_mismatched_hash_raises_by_default():
    """با hash متفاوت، پیش‌فرض باید ValueError بدهد."""
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=WRONG_HASH)
        try:
            load_state(state_dir)
            assert False, "expected ValueError on hash mismatch"
        except ValueError as e:
            msg = str(e).lower()
            assert "strategy" in msg or "hash" in msg, (
                f"unexpected error message: {e}"
            )


def test_mismatched_hash_can_be_forced():
    """strict=False صریح، پذیرش state قدیمی را ممکن می‌کند (بدون بازنویسی hash)."""
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=WRONG_HASH)
        loaded = load_state(state_dir, strict=False)
        assert loaded["strategy_hash"] == WRONG_HASH, (
            "strict=False نباید strategy_hash ذخیره‌شده را بازنویسی کند"
        )


def test_run_paper_exits_on_mismatch_no_position():
    """
    اجرای run_paper با hash mismatch و بدون پوزیشن:
      - exit code 1
      - state.json دست‌نخورده
      - events.jsonl (اگر نبود) ساخته نمی‌شود
    """
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=WRONG_HASH,
                                    with_position=False)
        state_path = os.path.join(state_dir, "state.json")
        events_path = os.path.join(state_dir, "events.jsonl")

        state_before = _read_bytes(state_path)
        events_before = _read_bytes(events_path)

        klines = _make_candles(300)
        exit_code, stdout = _run_main(state_dir, klines)

        state_after = _read_bytes(state_path)
        events_after = _read_bytes(events_path)

        assert exit_code == 1, f"expected exit 1, got {exit_code}"
        assert state_after == state_before, (
            "state.json modified on rejected run"
        )
        assert events_after == events_before, (
            f"events.jsonl modified on rejected run "
            f"(before={events_before!r}, after={events_after!r})"
        )
        _assert_mismatch_hint(stdout)


def test_run_paper_exits_on_mismatch_with_position():
    """
    اجرای run_paper با hash mismatch و پوزیشن باز:
      - exit code 1
      - state.json دست‌نخورده
      - پیام شامل اشاره‌ی صریح به پوزیشن باز و نیاز به اقدام دستی
    """
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=WRONG_HASH,
                                    with_position=True)
        state_path = os.path.join(state_dir, "state.json")
        state_before = _read_bytes(state_path)

        klines = _make_candles(300)
        exit_code, stdout = _run_main(state_dir, klines)

        state_after = _read_bytes(state_path)
        assert exit_code == 1, f"expected exit 1, got {exit_code}"
        assert state_after == state_before, (
            "state.json modified on rejected run"
        )
        _assert_mismatch_hint(stdout)
        _assert_position_hint(stdout)


def test_run_paper_proceeds_on_matching_hash():
    """
    با hash یکسان و وجود ۲ کندل جدید، هر دو باید پردازش شوند
    و آخرین ts ذخیره‌شده = آخرین کندل.
    """
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        # ۲ کندل جدید: 298 و 299
        state_dir = _make_state_dir(
            td, hash_override=None,
            last_processed=int(klines[297][0]),
        )
        exit_code, stdout = _run_main(state_dir, klines)
        assert exit_code in (0, None), (
            f"matching-hash run should succeed, got exit_code={exit_code}\n"
            f"stdout tail: {stdout[-500:]!r}"
        )
        with open(os.path.join(state_dir, "state.json")) as f:
            final = json.load(f)
        assert final["last_processed_candle_ts"] == int(klines[-1][0]), (
            f"expected {int(klines[-1][0])}, got "
            f"{final['last_processed_candle_ts']}"
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_default_load_is_strict,
        test_matching_hash_loads_normally,
        test_mismatched_hash_raises_by_default,
        test_mismatched_hash_can_be_forced,
        test_run_paper_exits_on_mismatch_no_position,
        test_run_paper_exits_on_mismatch_with_position,
        test_run_paper_proceeds_on_matching_hash,
    ]

    passed = 0
    failed = 0
    failures = []

    print("=" * 68)
    print(f"🧪 State Hash Strict Tests ({len(tests)} total)")
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