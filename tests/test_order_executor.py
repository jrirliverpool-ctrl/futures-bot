"""
test_order_executor.py — Contract tests برای order_executor.py.

⚠️ به‌روزرسانی قرارداد (v1.1):
  - EXIT حتی با فعال بودن Kill Switch مجاز است.
"""

import math
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_executor import (
    OrderRequest, Decision, evaluate, MockAdapter,
    DEFAULT_CONFIG, VALID_SIDES, VALID_ORDER_TYPES, VALID_INTENTS,
)

SPECS = {
    "contract_multiplier": 0.001, "min_qty": 0.0001, "max_qty": 120.0,
    "qty_step": 0.0001, "min_notional": 0.0, "tick_size": 0.1, "max_leverage": 200.0,
}

SPECS_WITH_NOTIONAL = dict(SPECS)
SPECS_WITH_NOTIONAL["min_notional"] = 5.0

STATE_EMPTY = {
    "position": None, "pending_entry": None,
    "circuit_breaker_active": False, "kill_switch": False,
}

STATE_WITH_POSITION = {
    "position": {"side": "LONG", "entry_price": 85000.0},
    "pending_entry": None, "circuit_breaker_active": False, "kill_switch": False,
}

def _req(**overrides) -> OrderRequest:
    base = {
        "symbol": "BTC-SWAP-USDT", "side": "LONG", "order_type": "MARKET",
        "qty": 0.001, "price": None, "leverage": 3, "intent": "ENTRY",
    }
    base.update(overrides)
    return OrderRequest(**base)

# Shape (5)
def test_shape_valid_accepts():
    d = evaluate(_req(), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is True

def test_shape_empty_symbol_rejects():
    d = evaluate(_req(symbol=""), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "shape.symbol"

def test_shape_invalid_side_rejects():
    d = evaluate(_req(side="SIDEWAYS"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "shape.side"

def test_shape_invalid_order_type_rejects():
    d = evaluate(_req(order_type="STOP"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "shape.order_type"

def test_shape_invalid_intent_rejects():
    d = evaluate(_req(intent="MAYBE"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "shape.intent"

# Qty (5)
def test_qty_below_min_rejects():
    d = evaluate(_req(qty=0.00001), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "qty"

def test_qty_above_max_rejects():
    d = evaluate(_req(qty=200.0), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "qty"

def test_qty_not_aligned_to_step_rejects():
    d = evaluate(_req(qty=0.00015), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "qty"

def test_qty_nan_rejects():
    d = evaluate(_req(qty=float("nan")), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "qty"

def test_qty_inf_rejects():
    d = evaluate(_req(qty=float("inf")), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "qty"

# Price (4)
def test_market_with_price_rejects():
    d = evaluate(_req(price=85000.0), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "price"

def test_limit_without_price_rejects():
    d = evaluate(_req(order_type="LIMIT", price=None), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "price"

def test_limit_price_not_aligned_to_tick_rejects():
    d = evaluate(_req(order_type="LIMIT", price=85000.05), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "price"

def test_limit_valid_price_accepts():
    d = evaluate(_req(order_type="LIMIT", price=85000.1), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is True

# Leverage (3)
def test_leverage_bool_rejects():
    d = evaluate(_req(leverage=True), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "leverage"

def test_leverage_above_cap_rejects():
    d = evaluate(_req(leverage=5), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "leverage"

def test_leverage_zero_rejects():
    d = evaluate(_req(leverage=0), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "leverage"

# Notional (2)
def test_notional_below_min_rejects():
    d = evaluate(_req(qty=0.0001), SPECS_WITH_NOTIONAL, STATE_EMPTY, DEFAULT_CONFIG, ref_price=85000.0)
    assert d.accepted is False and d.gate == "notional"

def test_notional_above_min_accepts():
    d = evaluate(_req(qty=0.1), SPECS_WITH_NOTIONAL, STATE_EMPTY, DEFAULT_CONFIG, ref_price=85000.0)
    assert d.accepted is True

# Kill switch (3)
def test_circuit_breaker_active_rejects():
    state = dict(STATE_EMPTY)
    state["circuit_breaker_active"] = True
    d = evaluate(_req(), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "kill_switch"

def test_kill_switch_flag_rejects():
    state = dict(STATE_EMPTY)
    state["kill_switch"] = True
    d = evaluate(_req(), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "kill_switch"

def test_exit_not_blocked_by_kill_switch_exception():
    # ✅ اصلاح: قرارداد جدید: EXIT حتی با kill switch فعال مجاز است
    state = dict(STATE_WITH_POSITION)
    state["circuit_breaker_active"] = True
    d = evaluate(_req(intent="EXIT"), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is True, f"EXIT should be allowed, got gate={d.gate}"
    assert d.order["intent"] == "EXIT"

# Duplicate (2)
def test_duplicate_entry_with_position_rejects():
    d = evaluate(_req(intent="ENTRY"), SPECS, STATE_WITH_POSITION, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "duplicate"

def test_duplicate_entry_with_pending_rejects():
    state = dict(STATE_EMPTY)
    state["pending_entry"] = {"side": "LONG"}
    d = evaluate(_req(intent="ENTRY"), SPECS, state, DEFAULT_CONFIG)
    assert d.accepted is False and d.gate == "duplicate"

# Position limit (2)
def test_position_limit_blocks_entry_when_full():
    d = evaluate(_req(intent="ENTRY"), SPECS, STATE_WITH_POSITION, DEFAULT_CONFIG)
    assert d.accepted is False

def test_exit_allowed_when_position_open():
    d = evaluate(_req(intent="EXIT"), SPECS, STATE_WITH_POSITION, DEFAULT_CONFIG)
    assert d.accepted is True
    assert d.order["intent"] == "EXIT"

# MockAdapter (3)
def test_mock_adapter_records_history():
    adapter = MockAdapter(ref_price=85000.0)
    adapter.submit(_req(), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    adapter.submit(_req(side="SHORT"), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    hist = adapter.history()
    assert len(hist) == 2

def test_mock_adapter_reset():
    adapter = MockAdapter()
    adapter.submit(_req(), SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert len(adapter.history()) == 1
    adapter.reset()
    assert len(adapter.history()) == 0

def test_mock_adapter_deterministic():
    a1 = MockAdapter(ref_price=85000.0)
    a2 = MockAdapter(ref_price=85000.0)
    req = _req()
    a1.submit(req, SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    a2.submit(req, SPECS, STATE_EMPTY, DEFAULT_CONFIG)
    assert a1.history() == a2.history()

# Constants
def test_constants():
    assert set(VALID_SIDES) == {"LONG", "SHORT"}
    assert set(VALID_ORDER_TYPES) == {"MARKET", "LIMIT"}
    assert set(VALID_INTENTS) == {"ENTRY", "EXIT"}

if __name__ == "__main__":
    tests = [
        test_shape_valid_accepts, test_shape_empty_symbol_rejects,
        test_shape_invalid_side_rejects, test_shape_invalid_order_type_rejects,
        test_shape_invalid_intent_rejects, test_qty_below_min_rejects,
        test_qty_above_max_rejects, test_qty_not_aligned_to_step_rejects,
        test_qty_nan_rejects, test_qty_inf_rejects, test_market_with_price_rejects,
        test_limit_without_price_rejects, test_limit_price_not_aligned_to_tick_rejects,
        test_limit_valid_price_accepts, test_leverage_bool_rejects,
        test_leverage_above_cap_rejects, test_leverage_zero_rejects,
        test_notional_below_min_rejects, test_notional_above_min_accepts,
        test_circuit_breaker_active_rejects, test_kill_switch_flag_rejects,
        test_exit_not_blocked_by_kill_switch_exception,
        test_duplicate_entry_with_position_rejects,
        test_duplicate_entry_with_pending_rejects,
        test_position_limit_blocks_entry_when_full,
        test_exit_allowed_when_position_open,
        test_mock_adapter_records_history, test_mock_adapter_reset,
        test_mock_adapter_deterministic, test_constants,
    ]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n🏁 {passed}/{len(tests)} PASSED")
    if failed: sys.exit(1)