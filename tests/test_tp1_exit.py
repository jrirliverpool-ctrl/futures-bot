"""
test_tp1_exit.py — Verification of TP1 partial exit logic and event parsing.
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
# ۲. تست تابع _create_order_from_event (مپ کردن کلیدها)
# ============================================================

def test_create_order_from_exit_event():
    """✅ تست پچ جدید: بررسی خواندن صحیح کلیدهای EXIT"""
    ev = {
        "intent": "EXIT",
        "side": "LONG",
        "qty": 16.8278,
        "price": 83870.78,
        "timestamp": 1792591200000
    }
    
    req, price, original_intent = _create_order_from_event(ev, "BTC-SWAP-USDT")
    
    assert original_intent == "EXIT"
    assert req.intent == "EXIT"
    assert req.qty == 16.8278
    assert price == 83870.78

def test_create_order_from_tp1_event():
    """✅ تست پچ جدید: بررسی خواندن صحیح کلیدهای TP1_HIT"""
    ev = {
        "intent": "TP1_HIT",
        "side": "LONG",
        "qty": 8.4139,
        "price": 84596.29,
        "timestamp": 1792591200000
    }
    
    req, price, original_intent = _create_order_from_event(ev, "BTC-SWAP-USDT")
    
    assert original_intent == "TP1_HIT"
    assert req.intent == "EXIT"
    assert req.qty == 8.4139

def test_tp1_event_accepted_by_gate():
    state = _state_with_position(size=1.0)
    ev = {
        "intent": "TP1_HIT",
        "side": "LONG",
        "qty": 0.5,
        "price": 50000.0,
    }
    req, price, _ = _create_order_from_event(ev, "BTC-SWAP-USDT")
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=price)
    assert decision.accepted is True
    assert decision.order["intent"] == "EXIT"

def test_tp1_event_rejected_if_over_position():
    state = _state_with_position(size=0.3)
    ev = {
        "intent": "TP1_HIT",
        "side": "LONG",
        "qty": 0.5,
        "price": 50000.0,
    }
    req, price, _ = _create_order_from_event(ev, "BTC-SWAP-USDT")
    decision = evaluate(req, SPECS, state, DEFAULT_CONFIG, ref_price=price)
    assert decision.accepted is False
    assert decision.gate == "exit_qty"