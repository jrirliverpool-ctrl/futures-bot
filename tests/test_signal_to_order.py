"""
test_signal_to_order.py — Contract tests برای signal_to_order.py.

هدف: قفل کردن قرارداد پل بین Strategy و Execution.

پوشش (16 تست):
  - signal_to_order (9)
  - signal_to_exit_order (7)
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from signal_to_order import (
    signal_to_order,
    signal_to_exit_order,
    DEFAULT_SIZING,
    VALID_SIGNALS,
)
from order_executor import OrderRequest


def _long_signal():
    return {"signal": "LONG", "symbol": "BTC-SWAP-USDT", "reason": "test"}


def _short_signal():
    return {"signal": "SHORT", "symbol": "BTC-SWAP-USDT", "reason": "test"}


def _no_trade_signal():
    return {"signal": "NO_TRADE", "symbol": "BTC-SWAP-USDT"}


def _state_with_position(side="LONG", qty=0.003, leverage=3,
                         symbol="BTC-SWAP-USDT"):
    return {
        "symbol": symbol,
        "position": {
            "side": side,
            "remaining_size": qty,
            "leverage": leverage,
            "entry_price": 85000.0,
        },
    }


# ============================================================
# signal_to_order — 9 tests
# ============================================================

def test_long_signal_creates_entry_order():
    req = signal_to_order(_long_signal())
    assert isinstance(req, OrderRequest)
    assert req.side == "LONG"
    assert req.intent == "ENTRY"
    assert req.symbol == "BTC-SWAP-USDT"
    assert req.order_type == "MARKET"
    assert req.price is None
    print("✅ test_long_signal_creates_entry_order")


def test_short_signal_creates_entry_order():
    req = signal_to_order(_short_signal())
    assert isinstance(req, OrderRequest)
    assert req.side == "SHORT"
    assert req.intent == "ENTRY"
    print("✅ test_short_signal_creates_entry_order")


def test_no_trade_returns_none():
    assert signal_to_order(_no_trade_signal()) is None
    print("✅ test_no_trade_returns_none")


def test_invalid_signal_raises():
    try:
        signal_to_order({"signal": "MAYBE"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_invalid_signal_raises")


def test_signal_not_dict_raises():
    try:
        signal_to_order("LONG")
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_signal_not_dict_raises")


def test_signal_with_none_symbol_raises():
    try:
        signal_to_order({"signal": "LONG", "symbol": None})
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_signal_with_none_symbol_raises")


def test_signal_without_symbol_raises():
    try:
        signal_to_order({"signal": "LONG"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_signal_without_symbol_raises")


def test_explicit_symbol_overrides_signal():
    req = signal_to_order(
        _long_signal(),
        explicit_symbol="ETH-SWAP-USDT",
    )
    assert req.symbol == "ETH-SWAP-USDT"
    print("✅ test_explicit_symbol_overrides_signal")


def test_sizing_invalid_type_raises():
    try:
        signal_to_order(_long_signal(), sizing="not a dict")
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_sizing_invalid_type_raises")


# ============================================================
# signal_to_exit_order — 7 tests
# ============================================================

def test_exit_uses_remaining_size_not_sizing():
    state = _state_with_position(qty=0.003)
    req = signal_to_exit_order(state)
    assert req is not None
    assert req.qty == 0.003, (
        f"expected qty=0.003 (remaining_size), got {req.qty}"
    )
    print("✅ test_exit_uses_remaining_size_not_sizing")


def test_exit_with_large_position():
    state = _state_with_position(qty=12.5)
    req = signal_to_exit_order(state)
    assert req.qty == 12.5
    print("✅ test_exit_with_large_position")


def test_exit_no_position_returns_none():
    assert signal_to_exit_order({"symbol": "X"}) is None
    print("✅ test_exit_no_position_returns_none")


def test_exit_side_matches_position_side():
    state_long = _state_with_position(side="LONG")
    state_short = _state_with_position(side="SHORT")

    req_long = signal_to_exit_order(state_long)
    req_short = signal_to_exit_order(state_short)

    assert req_long.side == "LONG"
    assert req_long.intent == "EXIT"
    assert req_short.side == "SHORT"
    assert req_short.intent == "EXIT"
    print("✅ test_exit_side_matches_position_side")


def test_exit_missing_symbol_raises():
    state = {
        "position": {
            "side": "LONG",
            "remaining_size": 0.001,
            "leverage": 3,
        }
    }
    try:
        signal_to_exit_order(state)
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_exit_missing_symbol_raises")


def test_exit_invalid_remaining_size_raises():
    state = _state_with_position()
    state["position"]["remaining_size"] = 0.0
    try:
        signal_to_exit_order(state)
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_exit_invalid_remaining_size_raises")


def test_exit_state_not_dict_raises():
    try:
        signal_to_exit_order("not a dict")
        assert False, "expected ValueError"
    except ValueError:
        pass
    print("✅ test_exit_state_not_dict_raises")


if __name__ == "__main__":
    tests = [
        test_long_signal_creates_entry_order,
        test_short_signal_creates_entry_order,
        test_no_trade_returns_none,
        test_invalid_signal_raises,
        test_signal_not_dict_raises,
        test_signal_with_none_symbol_raises,
        test_signal_without_symbol_raises,
        test_explicit_symbol_overrides_signal,
        test_sizing_invalid_type_raises,
        test_exit_uses_remaining_size_not_sizing,
        test_exit_with_large_position,
        test_exit_no_position_returns_none,
        test_exit_side_matches_position_side,
        test_exit_missing_symbol_raises,
        test_exit_invalid_remaining_size_raises,
        test_exit_state_not_dict_raises,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Signal → Order Translator Contract Tests")
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