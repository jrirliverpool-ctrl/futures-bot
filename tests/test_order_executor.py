"""
test_order_executor.py — Contract tests برای order_executor.py.

هدف: قفل کردن قرارداد Execution Core در فاز Dry-Run.

پوشش (۳۰ تست):
  - Shape validation (5)
  - Qty validation (5)
  - Price validation (4)
  - Leverage validation (3)
  - Notional validation (2)
  - Kill switch (2)
  - Duplicate (2)
  - Position limit — ENTRY vs EXIT (3)
  - MockAdapter (3)
  - Constants (1)
"""

import math
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_executor import (
    OrderRequest,
    Decision,
    evaluate,
    MockAdapter,
    DEFAULT_CONFIG,
    VALID_SIDES,
    VALID_ORDER_TYPES,
    VALID_INTENTS,
)


# ============================================================
# Fixtures
# ============================================================

SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}

SPECS_WITH_NOTIONAL = dict(SPECS)
SPECS_WITH_NOTIONAL["min_notional"] = 5.0

STATE_EMPTY = {
    "position": None,
    "pending_entry": None,
    "circuit_breaker_active": False,
    "kill_switch": False,
}

STATE_WITH_POSITION = {
    "position": {"side": "LONG", "entry_price": 85000.0},
    "pending_entry": None,
    "circuit_breaker_active": False,
    "kill_switch": False,
}


def _req(**overrides) -> OrderRequest:
    """ساخت OrderRequest معتبر + overrides."""
    base = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "order_type": "MARKET",
        "qty": 0.001,
        "price": None,
        "leverage": 3,
        "intent": "ENTRY",
    }
    base.update(overrides)
    return OrderRequest(**base)


# ============================================================
# Shape Validation (5 tests)
# ============================================================

def test_shape_valid_accepts():
    d = evaluate(_req(), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is True
    assert d.gate is None
    assert d.order is not None
    assert d.order["symbol"] == "BTC-SWAP-USDT"
    print("✅ test_shape_valid_accepts")


def test_shape_empty_symbol_rejects():
    d = evaluate(_req(symbol=""), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "shape.symbol"
    print("✅ test_shape_empty_symbol_rejects")


def test_shape_invalid_side_rejects():
    d = evaluate(_req(side="SIDEWAYS"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "shape.side"
    print("✅ test_shape_invalid_side_rejects")


def test_shape_invalid_order_type_rejects():
    """order_type=STOP نامعتبر است — فقط MARKET و LIMIT."""
    d = evaluate(_req(order_type="STOP"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "shape.order_type"
    print("✅ test_shape_invalid_order_type_rejects")


def test_shape_invalid_intent_rejects():
    d = evaluate(_req(intent="MAYBE"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "shape.intent"
    print("✅ test_shape_invalid_intent_rejects")


# ============================================================
# Qty Validation (5 tests)
# ============================================================

def test_qty_below_min_rejects():
    d = evaluate(_req(qty=0.00001), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "qty"
    print("✅ test_qty_below_min_rejects")


def test_qty_above_max_rejects():
    d = evaluate(_req(qty=200.0), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "qty"
    print("✅ test_qty_above_max_rejects")


def test_qty_not_aligned_to_step_rejects():
    d = evaluate(_req(qty=0.00015), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "qty"
    print("✅ test_qty_not_aligned_to_step_rejects")


def test_qty_nan_rejects():
    d = evaluate(_req(qty=float("nan")), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "qty"
    print("✅ test_qty_nan_rejects")


def test_qty_inf_rejects():
    d = evaluate(_req(qty=float("inf")), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "qty"
    print("✅ test_qty_inf_rejects")


# ============================================================
# Price Validation (4 tests)
# ============================================================

def test_market_with_price_rejects():
    d = evaluate(_req(price=85000.0), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "price"
    print("✅ test_market_with_price_rejects")


def test_limit_without_price_rejects():
    d = evaluate(_req(order_type="LIMIT", price=None),
                 SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "price"
    print("✅ test_limit_without_price_rejects")


def test_limit_price_not_aligned_to_tick_rejects():
    d = evaluate(_req(order_type="LIMIT", price=85000.05),
                 SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "price"
    print("✅ test_limit_price_not_aligned_to_tick_rejects")


def test_limit_valid_price_accepts():
    d = evaluate(_req(order_type="LIMIT", price=85000.1),
                 SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is True
    print("✅ test_limit_valid_price_accepts")


# ============================================================
# Leverage Validation (3 tests)
# ============================================================

def test_leverage_bool_rejects():
    d = evaluate(_req(leverage=True), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "leverage"
    print("✅ test_leverage_bool_rejects")


def test_leverage_above_cap_rejects():
    d = evaluate(_req(leverage=5), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "leverage"
    print("✅ test_leverage_above_cap_rejects")


def test_leverage_zero_rejects():
    d = evaluate(_req(leverage=0), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "leverage"
    print("✅ test_leverage_zero_rejects")


# ============================================================
# Notional Validation (2 tests)
# ============================================================

def test_notional_below_min_rejects():
    d = evaluate(_req(qty=0.0001), SPECS_WITH_NOTIONAL,
                 STATE_EMPTY, DEFAULT_CONFIG, ref_price=85000.0)
    assert d.accepted is False
    assert d.gate == "notional"
    print("✅ test_notional_below_min_rejects")


def test_notional_above_min_accepts():
    d = evaluate(_req(qty=0.1), SPECS_WITH_NOTIONAL,
                 STATE_EMPTY, DEFAULT_CONFIG, ref_price=85000.0)
    assert d.accepted is True
    print("✅ test_notional_above_min_accepts")


# ============================================================
# Kill Switch (2 tests)
# ============================================================

def test_circuit_breaker_active_rejects():
    state = dict(STATE_EMPTY)
    state["circuit_breaker_active"] = True
    d = evaluate(_req(), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "kill_switch"
    print("✅ test_circuit_breaker_active_rejects")


def test_kill_switch_flag_rejects():
    state = dict(STATE_EMPTY)
    state["kill_switch"] = True
    d = evaluate(_req(), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "kill_switch"
    print("✅ test_kill_switch_flag_rejects")


# ============================================================
# Duplicate (2 tests)
# ============================================================

def test_duplicate_entry_with_position_rejects():
    d = evaluate(_req(intent="ENTRY"), SPECS,
                 STATE_WITH_POSITION, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "duplicate"
    print("✅ test_duplicate_entry_with_position_rejects")


def test_duplicate_entry_with_pending_rejects():
    state = dict(STATE_EMPTY)
    state["pending_entry"] = {"side": "LONG"}
    d = evaluate(_req(intent="ENTRY"), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "duplicate"
    print("✅ test_duplicate_entry_with_pending_rejects")


# ============================================================
# Position Limit — ENTRY vs EXIT (3 tests)
# ============================================================

def test_position_limit_blocks_entry_when_full():
    d = evaluate(_req(intent="ENTRY"), SPECS,
                 STATE_WITH_POSITION, DEFAULT_CONFIG)
    assert d.accepted is False
    print("✅ test_position_limit_blocks_entry_when_full")


def test_exit_allowed_when_position_open():
    """
    EXIT با پوزیشن باز → باید قبول شود.
    position_limit نباید مسیر EXIT را ببندد.
    """
    d = evaluate(_req(intent="EXIT"), SPECS,
                 STATE_WITH_POSITION, DEFAULT_CONFIG)
    assert d.accepted is True, (
        f"EXIT rejected — gate={d.gate} reason={d.reason}"
    )
    assert d.order["intent"] == "EXIT"
    print("✅ test_exit_allowed_when_position_open")


def test_exit_not_blocked_by_kill_switch_exception():
    """قرارداد فعلی: EXIT هم توسط kill_switch بسته می‌شود."""
    state = dict(STATE_WITH_POSITION)
    state["circuit_breaker_active"] = True
    d = evaluate(_req(intent="EXIT"), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False
    assert d.gate == "kill_switch"
    print("✅ test_exit_not_blocked_by_kill_switch_exception")


# ============================================================
# MockAdapter (3 tests)
# ============================================================

def test_mock_adapter_records_history():
    adapter = MockAdapter(ref_price=85000.0)
    adapter.submit(_req(), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    adapter.submit(_req(side="SHORT"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)

    hist = adapter.history()
    assert len(hist) == 2
    assert hist[0]["request"]["side"] == "LONG"
    assert hist[1]["request"]["side"] == "SHORT"
    assert all(c["accepted"] for c in hist)
    print("✅ test_mock_adapter_records_history")


def test_mock_adapter_reset():
    adapter = MockAdapter()
    adapter.submit(_req(), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert len(adapter.history()) == 1
    adapter.reset()
    assert len(adapter.history()) == 0
    print("✅ test_mock_adapter_reset")


def test_mock_adapter_deterministic():
    a1 = MockAdapter(ref_price=85000.0)
    a2 = MockAdapter(ref_price=85000.0)

    req = _req()
    a1.submit(req, SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    a2.submit(req, SPECS, STATE_EMPTY, DEFAULT_CONFIG)

    assert a1.history() == a2.history()
    print("✅ test_mock_adapter_deterministic")


# ============================================================
# Constants
# ============================================================

def test_constants():
    assert set(VALID_SIDES) == {"LONG", "SHORT"}
    assert set(VALID_ORDER_TYPES) == {"MARKET", "LIMIT"}
    assert set(VALID_INTENTS) == {"ENTRY", "EXIT"}
    print("✅ test_constants")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Shape (5)
        test_shape_valid_accepts,
        test_shape_empty_symbol_rejects,
        test_shape_invalid_side_rejects,
        test_shape_invalid_order_type_rejects,
        test_shape_invalid_intent_rejects,
        # Qty (5)
        test_qty_below_min_rejects,
        test_qty_above_max_rejects,
        test_qty_not_aligned_to_step_rejects,
        test_qty_nan_rejects,
        test_qty_inf_rejects,
        # Price (4)
        test_market_with_price_rejects,
        test_limit_without_price_rejects,
        test_limit_price_not_aligned_to_tick_rejects,
        test_limit_valid_price_accepts,
        # Leverage (3)
        test_leverage_bool_rejects,
        test_leverage_above_cap_rejects,
        test_leverage_zero_rejects,
        # Notional (2)
        test_notional_below_min_rejects,
        test_notional_above_min_accepts,
        # Kill switch (2)
        test_circuit_breaker_active_rejects,
        test_kill_switch_flag_rejects,
        # Duplicate (2)
        test_duplicate_entry_with_position_rejects,
        test_duplicate_entry_with_pending_rejects,
        # Position limit (3)
        test_position_limit_blocks_entry_when_full,
        test_exit_allowed_when_position_open,
        test_exit_not_blocked_by_kill_switch_exception,
        # MockAdapter (3)
        test_mock_adapter_records_history,
        test_mock_adapter_reset,
        test_mock_adapter_deterministic,
        # Constants (1)
        test_constants,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Order Executor Contract Tests")
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