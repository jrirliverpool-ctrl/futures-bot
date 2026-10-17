"""
test_state_hash_strict.py — تست سیاست strict در بارگذاری state
و رفتار fail-safe در مدیریت ATR.

قرارداد:
  - paper_state_manager.load_state پیش‌فرض strict=True
  - run_paper هنگام hash mismatch با پیام صریح exit(1) می‌دهد
  - state.json و events.jsonl در rejection دست‌نخورده می‌مانند
  - پوزیشن باز در state، هشدار diag صریح می‌گیرد
  - paper_engine هنگام ATR=None از atr_at_entry استفاده می‌کند
  - اگر هر دو ATR نامعتبر باشند، ValueError با پیام روشن

v2.3 (سازگاری با main()->int):
  _run_main مقدار int بازگشتی main() را می‌گیرد.
  bool به‌عنوان exit code پذیرفته نمی‌شود (type() is int، نه isinstance
  — چون در پایتون isinstance(True, int) == True است).
  اگر SystemExit رخ داد (سازگاری عقب‌رو)، code را از آن می‌گیرد.

v2.4:
  تست موفق حالا سخت‌گیرانه است: exit_code باید دقیقاً 0 باشد،
  نه None. این تضمین می‌کند main() که مقدار برنمی‌گرداند،
  به‌عنوان موفق پذیرفته نشود.
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
from paper_engine import manage_position_on_candle


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


def _make_position_fixture(atr_at_entry=1.0):
    return {
        "side": "LONG", "entry_price": 100.0,
        "entry_candle_ts": BASE_TS, "size": 1.0,
        "remaining_size": 1.0, "tp1_size": 0.0,
        "realized_pnl": 0.0, "initial_sl": 98.0,
        "current_sl": 98.0, "initial_tp1": 104.0,
        "tp1_hit": False, "be_moved": False,
        "trailing_active": False,
        "highest_high_since_entry": 100.0,
        "lowest_low_since_entry": 100.0,
        "candles_since_entry": 0, "atr_at_entry": atr_at_entry,
        "signal_candle_ts": BASE_TS - HOUR,
    }


def _engine_config_fixture():
    return {
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 2.0,
        "trailing_atr_mult": 1.5,
        "be_sl_offset_atr": 0.1,
        "be_trigger_at_r": 1.0,
        "time_exit_candles": 48,
        "contract_multiplier_for_pnl": 1.0,
    }


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
        state["position"] = _make_position_fixture()
    _write_state(state_dir, state)
    return state_dir


def _read_bytes(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _run_main(state_dir, klines):
    """
    اجرای rp.main() با mockهای لازم.

    v2.3: main() حالا int برمی‌گرداند (نه SystemExit).
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
                # type() is int — نه isinstance — چون isinstance(True, int)
                # در پایتون True است.
                exit_code = result if type(result) is int else None
            except SystemExit as e:
                exit_code = e.code
    return exit_code, buf.getvalue()


def _assert_mismatch_hint(stdout):
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
# Hash strict tests (7)
# ============================================================

def test_default_load_is_strict():
    sig = inspect.signature(load_state)
    default = sig.parameters["strict"].default
    assert default is True, (
        f"load_state strict default is {default!r}, expected True"
    )


def test_matching_hash_loads_normally():
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=None)
        loaded = load_state(state_dir)
        assert loaded["strategy_hash"] == default_state()["strategy_hash"]


def test_mismatched_hash_raises_by_default():
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
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(td, hash_override=WRONG_HASH)
        loaded = load_state(state_dir, strict=False)
        assert loaded["strategy_hash"] == WRONG_HASH, (
            "strict=False نباید strategy_hash ذخیره‌شده را بازنویسی کند"
        )


def test_run_paper_exits_on_mismatch_no_position():
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
    with tempfile.TemporaryDirectory() as td:
        state_dir = _make_state_dir(
            td, hash_override=WRONG_HASH, with_position=True,
            last_processed=None,
        )
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
    with tempfile.TemporaryDirectory() as td:
        klines = _make_candles(300)
        state_dir = _make_state_dir(
            td, hash_override=None,
            last_processed=int(klines[297][0]),
        )
        exit_code, stdout = _run_main(state_dir, klines)

        assert exit_code == 0, (
            f"matching-hash run must exit 0, got exit_code={exit_code}\n"
            f"stdout tail: {stdout[-500:]!r}"
        )
        with open(os.path.join(state_dir, "state.json")) as f:
            final = json.load(f)
        assert final["last_processed_candle_ts"] == int(klines[-1][0]), (
            f"expected {int(klines[-1][0])}, got "
            f"{final['last_processed_candle_ts']}"
        )


# ============================================================
# ATR fail-safe tests (2)
# ============================================================

def test_paper_engine_fallback_when_atr_current_none():
    """ATR=None ولی atr_at_entry معتبر → fallback، بدون استثنا."""
    pos = _make_position_fixture(atr_at_entry=1.0)
    candle = [BASE_TS + HOUR, 100.0, 100.5, 99.5, 100.0, 1000.0]
    cfg = _engine_config_fixture()

    new_pos, events, exited = manage_position_on_candle(
        pos, candle, atr_current=None, config=cfg,
    )
    assert isinstance(new_pos, dict)
    assert isinstance(events, list)


def test_paper_engine_raises_when_both_atr_invalid():
    """ATR=None و atr_at_entry=None → ValueError با پیام روشن."""
    pos = _make_position_fixture(atr_at_entry=None)
    candle = [BASE_TS + HOUR, 100.0, 100.5, 99.5, 100.0, 1000.0]
    cfg = _engine_config_fixture()

    try:
        manage_position_on_candle(
            pos, candle, atr_current=None, config=cfg,
        )
        assert False, "expected ValueError when both ATRs invalid"
    except ValueError as e:
        msg = str(e).lower()
        assert "atr" in msg, (
            f"error message باید صریحاً به ATR اشاره کند: {e}"
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Hash strict (7)
        test_default_load_is_strict,
        test_matching_hash_loads_normally,
        test_mismatched_hash_raises_by_default,
        test_mismatched_hash_can_be_forced,
        test_run_paper_exits_on_mismatch_no_position,
        test_run_paper_exits_on_mismatch_with_position,
        test_run_paper_proceeds_on_matching_hash,
        # ATR fail-safe (2)
        test_paper_engine_fallback_when_atr_current_none,
        test_paper_engine_raises_when_both_atr_invalid,
    ]

    passed = 0
    failed = 0
    failures = []

    print("=" * 68)
    print(f"🧪 State Hash Strict + ATR Fail-Safe Tests ({len(tests)} total)")
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