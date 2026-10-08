"""
test_execution_chain.py — Integration test برای زنجیره‌ی Execution.

هدف: اثبات اتصال:
    signal → OrderRequest → Decision

این فایل هیچ کد production جدیدی معرفی نمی‌کند.
فقط لایه‌های موجود را به هم وصل می‌کند.

قراردادهای صریح:
  - TEST_CONFIG با مقادیر صریح (نه DEFAULT_CONFIG پنهان)
  - leverage cap = 3 (ApexQuant V1 cap)
  - max_concurrent_positions = 1

پوشش (11 تست):
  - LONG → accept
  - SHORT → accept
  - NO_TRADE → None
  - ENTRY با پوزیشن باز → duplicate
  - EXIT با پوزیشن باز → accept
  - leverage = cap → accept (boundary)
  - leverage > cap → reject
  - kill_switch → reject
  - determinism
  - MockAdapter ثبت می‌کند
  - entry → exit decision path
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from signal_to_order import signal_to_order, signal_to_exit_order
from order_executor import evaluate, MockAdapter


# ============================================================
# Fixtures — صریح، بدون تکیه بر DEFAULT_CONFIG
# ============================================================

SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,    # exchange max
}

TEST_CONFIG = {
    "leverage_cap": 3,                 # ApexQuant V1 cap
    "max_concurrent_positions": 1,
}

STATE_EMPTY = {
    "symbol": "BTC-SWAP-USDT",
    "position": None,
    "pending_entry": None,
    "circuit_breaker_active": False,
    "kill_switch": False,
}

STATE_WITH_LONG = {
    "symbol": "BTC-SWAP-USDT",
    "position": {
        "side": "LONG",
        "entry_price": 85000.0,
        "remaining_size": 0.001,
        "leverage": 3,
    },
    "pending_entry": None,
    "circuit_breaker_active": False,
    "kill_switch": False,
}


def _long_signal():
    return {"signal": "LONG", "symbol": "BTC-SWAP-USDT"}


def _short_signal():
    return {"signal": "SHORT", "symbol": "BTC-SWAP-USDT"}


def _no_trade():
    return {"signal": "NO_TRADE", "symbol": "BTC-SWAP-USDT"}


def _sizing(qty=0.001, leverage=3):
    return {"qty": qty, "leverage": leverage}


# ============================================================
# Chain tests
# ============================================================

def test_chain_long_entry_accepted():
    req = signal_to_order(_long_signal(), sizing=_sizing())
    assert req is not None

    d = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                 ref_price=85000.0)
    assert d.accepted is True
    assert d.order["side"] == "LONG"
    assert d.order["intent"] == "ENTRY"
    print("✅ test_chain_long_entry_accepted")


def test_chain_short_entry_accepted():
    req = signal_to_order(_short_signal(), sizing=_sizing())
    d = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                 ref_price=85000.0)
    assert d.accepted is True
    assert d.order["side"] == "SHORT"
    print("✅ test_chain_short_entry_accepted")


def test_chain_no_trade_returns_no_order():
    req = signal_to_order(_no_trade(), sizing=_sizing())
    assert req is None
    print("✅ test_chain_no_trade_returns_no_order")


def test_chain_duplicate_blocked():
    req = signal_to_order(_long_signal(), sizing=_sizing())
    assert req is not None

    d = evaluate(req, SPECS, STATE_WITH_LONG, TEST_CONFIG,
                 ref_price=85000.0)
    assert d.accepted is False
    assert d.gate == "duplicate"
    print("✅ test_chain_duplicate_blocked")


def test_chain_exit_accepted_with_position():
    req = signal_to_exit_order(STATE_WITH_LONG)
    assert req is not None
    assert req.intent == "EXIT"

    d = evaluate(req, SPECS, STATE_WITH_LONG, TEST_CONFIG,
                 ref_price=85000.0)
    assert d.accepted is True
    print("✅ test_chain_exit_accepted_with_position")


def test_chain_leverage_at_cap_accepted():
    """
    Boundary: leverage = cap → باید accept شود.
    TEST_CONFIG دارای leverage_cap = 3 است.
    """
    req = signal_to_order(_long_signal(), sizing=_sizing(leverage=3))
    d = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                 ref_price=85000.0)
    assert d.accepted is True, (
        f"leverage=cap باید accept شود، got gate={d.gate}"
    )
    print("✅ test_chain_leverage_at_cap_accepted")


def test_chain_leverage_above_cap_rejected():
    """
    Boundary: leverage > cap → باید reject شود با gate="leverage".
    TEST_CONFIG دارای leverage_cap = 3 است.
    """
    req = signal_to_order(_long_signal(), sizing=_sizing(leverage=4))
    d = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                 ref_price=85000.0)
    assert d.accepted is False
    assert d.gate == "leverage", (
        f"leverage=cap+1 باید reject شود با gate=leverage، got gate={d.gate}"
    )
    print("✅ test_chain_leverage_above_cap_rejected")


def test_chain_kill_switch_blocks():
    state = dict(STATE_EMPTY)
    state["kill_switch"] = True

    req = signal_to_order(_long_signal(), sizing=_sizing())
    d = evaluate(req, SPECS, state, TEST_CONFIG, ref_price=85000.0)
    assert d.accepted is False
    assert d.gate == "kill_switch"
    print("✅ test_chain_kill_switch_blocks")


def test_chain_determinism():
    req = signal_to_order(_long_signal(), sizing=_sizing())

    d1 = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                  ref_price=85000.0)
    d2 = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                  ref_price=85000.0)

    assert d1 == d2
    print("✅ test_chain_determinism")


def test_chain_mock_adapter_full():
    adapter = MockAdapter(ref_price=85000.0)
    req = signal_to_order(_long_signal(), sizing=_sizing())

    d = adapter.submit(req, SPECS, STATE_EMPTY, TEST_CONFIG)
    assert d.accepted is True

    hist = adapter.history()
    assert len(hist) == 1
    assert hist[0]["accepted"] is True
    assert hist[0]["request"]["side"] == "LONG"
    print("✅ test_chain_mock_adapter_full")


def test_chain_entry_then_exit_decision_path():
    """
    مسیر تصمیم ENTRY و سپس EXIT.

    این یک چرخه‌ی معاملاتی واقعی نیست، فقط دو Decision
    مستقل در دو state متفاوت. برای اثبات اتصال زنجیره کافی است.
    """
    # 1. ورود
    req_entry = signal_to_order(_long_signal(), sizing=_sizing())
    d_entry = evaluate(req_entry, SPECS, STATE_EMPTY, TEST_CONFIG,
                       ref_price=85000.0)
    assert d_entry.accepted is True
    assert d_entry.order["intent"] == "ENTRY"

    # 2. خروج (state فرضی با پوزیشن باز)
    req_exit = signal_to_exit_order(STATE_WITH_LONG)
    d_exit = evaluate(req_exit, SPECS, STATE_WITH_LONG, TEST_CONFIG,
                      ref_price=86000.0)
    assert d_exit.accepted is True
    assert d_exit.order["intent"] == "EXIT"
    print("✅ test_chain_entry_then_exit_decision_path")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_chain_long_entry_accepted,
        test_chain_short_entry_accepted,
        test_chain_no_trade_returns_no_order,
        test_chain_duplicate_blocked,
        test_chain_exit_accepted_with_position,
        test_chain_leverage_at_cap_accepted,
        test_chain_leverage_above_cap_rejected,
        test_chain_kill_switch_blocks,
        test_chain_determinism,
        test_chain_mock_adapter_full,
        test_chain_entry_then_exit_decision_path,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Execution Chain Integration Tests")
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