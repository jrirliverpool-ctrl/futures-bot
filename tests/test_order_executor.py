"""
test_order_executor.py — Unit tests for order_executor.py.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from order_executor import OrderRequest, evaluate, DEFAULT_CONFIG

SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}

def _req(**kwargs):
    defaults = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "order_type": "MARKET",
        "qty": 1.0,
        "price": None,
        "leverage": 3,
        "intent": "ENTRY"
    }
    defaults.update(kwargs)
    return OrderRequest(**defaults)

def test_entry_valid():
    d = evaluate(_req(), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is True
    assert d.gate is None

def test_invalid_side():
    d = evaluate(_req(side="INVALID"), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "shape.side"

def test_invalid_intent():
    d = evaluate(_req(intent="INVALID"), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "shape.intent"

def test_qty_below_min():
    d = evaluate(_req(qty=0.00001), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "qty"

def test_qty_above_max():
    d = evaluate(_req(qty=200.0), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "qty"

def test_qty_not_multiple_of_step():
    d = evaluate(_req(qty=1.00005), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "qty"

def test_leverage_exceeds_cap():
    d = evaluate(_req(leverage=5), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "leverage"

def test_market_order_with_price_rejects():
    d = evaluate(_req(price=100.0), SPECS, {}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "price"

def test_exit_without_position_rejects():
    d = evaluate(_req(intent="EXIT"), SPECS, {"position": None}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "exit"

def test_exit_wrong_side_rejects():
    state = {"position": {"side": "LONG", "remaining_size": 1.0}}
    d = evaluate(_req(intent="EXIT", side="SHORT"), SPECS, state, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert "side" in d.reason

def test_exit_qty_too_large_rejects():
    state = {"position": {"side": "LONG", "remaining_size": 0.5}}
    d = evaluate(_req(intent="EXIT", side="LONG", qty=1.0), SPECS, state, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert "qty" in d.reason

def test_tp1_hit_valid():
    state = {"position": {"side": "LONG", "remaining_size": 1.0}}
    d = evaluate(_req(intent="TP1_HIT", side="LONG", qty=0.5), SPECS, state, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is True

def test_halt_trading_blocks_entry():
    d = evaluate(_req(), SPECS, {"halt_trading": True}, DEFAULT_CONFIG, ref_price=100.0)
    assert d.accepted is False
    assert d.gate == "kill_switch"