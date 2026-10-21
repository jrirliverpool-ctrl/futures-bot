"""
test_tp1_exit.py — Verification of TP1 partial exit logic.
"""
import os
import sys
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

from order_executor import OrderRequest, evaluate, DEFAULT_CONFIG
from run_mock_live import _create_order_from_event

SPECS = {
    "min_qty": 0.01, "max_qty": 100.0, "qty_step": 0.01,
    "tick_size": 0.1, "min_notional": 10.0,
    "contract_multiplier": 1.0, "max_leverage": 10
}

def _state_with_position(size=1.0):
    return {
        "position": {"side": "LONG", "size": size, "remaining_size": size},
        "pending_entry": None, "circuit_breaker_active": False, "kill_switch": False
    }

# ============================================================
# ۱. تست‌های order_executor (گیت exit_qty)
# ============================================================

def test_exit_valid_partial_qty():
    state = _state_with_position(size=1.0)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.5, None, 1, "EXIT")
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True

def test_exit_over_position_rejected():
    state = _state_with_position(size=1.0)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 1.5, None, 1, "EXIT")
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.gate == "exit_qty"

def test_exit_without_position_rejected():
    state = {"position": None, "pending_entry": None, "kill_switch": False}
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.5, None, 1, "EXIT")
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.gate == "exit_qty"

# ============================================================
# ۲. تست تابع _create_order_from_event (مپ کردن TP1_HIT)
# ============================================================

def test_create_order_maps_tp1_to_exit():
    # رویداد TP1_HIT که paper_engine تولید می‌کند
    ev = {
        "type": "TP1_HIT",
        "side": "LONG",
        "size": 0.5, # حجم جزئی
        "price": 50000.0,
    }
    
    req, price, original_intent = _create_order_from_event(ev, "BTC-SWAP-USDT")
    
    assert original_intent == "TP1_HIT"
    assert req.intent == "EXIT" # ✅ مپ شده
    assert req.qty == 0.5 # ✅ حجم جزئی حفظ شده
    assert price == 50000.0

def test_tp1_event_accepted_by_gate():
    state = _state_with_position(size=1.0)
    
    ev = {
        "type": "TP1_HIT",
        "side": "LONG",
        "size": 0.5,
        "price": 50000.0,
    }
    
    # تبدیل به OrderRequest
    req, price, _ = _create_order_from_event(ev, "BTC-SWAP-USDT")
    
    # اعتبارسنجی
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=price)
    
    assert decision.accepted is True
    assert decision.order["intent"] == "EXIT"

def test_tp1_event_rejected_if_over_position():
    state = _state_with_position(size=0.3) # حجم پوزیشن کم است
    
    ev = {
        "type": "TP1_HIT",
        "side": "LONG",
        "size": 0.5, # حجم خروج بیشتر از پوزیشن
        "price": 50000.0,
    }
    
    req, price, _ = _create_order_from_event(ev, "BTC-SWAP-USDT")
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=price)
    
    assert decision.accepted is False
    assert decision.gate == "exit_qty"