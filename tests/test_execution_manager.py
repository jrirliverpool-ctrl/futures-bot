"""
test_execution_manager.py — Unit tests for execution_manager.py.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from execution_manager import ExecutionManager
from order_executor import OrderRequest

SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.1,
    "max_qty": 100.0,
    "qty_step": 0.1,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 10,
}

def _req():
    return OrderRequest(symbol="BTC-SWAP-USDT", side="LONG", order_type="MARKET", qty=1.0, price=None, leverage=3, intent="ENTRY")

class MockClient:
    def __init__(self, resp=None, raise_exc=None):
        self.resp = resp
        self.raise_exc = raise_exc
    def place_order(self, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        return self.resp

def test_halt_on_unknown_state():
    em = ExecutionManager("MOCK_LIVE")
    state = {"order_status": "UNKNOWN"}
    d = em.execute(_req(), SPECS, state)
    assert d.accepted is False
    assert d.reason == "ambiguous_order_halt"

def test_mock_live_accepts_and_clears_id():
    em = ExecutionManager("MOCK_LIVE")
    state = {}
    d = em.execute(_req(), SPECS, state)
    assert d.accepted is True
    assert state.get("pending_order_id") is None

def test_live_network_error_halts_system():
    em = ExecutionManager("LIVE", client=MockClient({"status": "NETWORK_ERROR"}))
    state = {}
    d = em.execute(_req(), SPECS, state)
    assert d.accepted is False
    assert state["halt_trading"] is True
    assert state["order_status"] == "UNKNOWN"

def test_live_network_exception_halts_system():
    em = ExecutionManager("LIVE", client=MockClient(raise_exc=Exception("Network down")))
    state = {}
    d = em.execute(_req(), SPECS, state)
    assert d.accepted is False
    assert state["halt_trading"] is True
    assert state["order_status"] == "UNKNOWN"

def test_live_api_error_does_not_halt():
    em = ExecutionManager("LIVE", client=MockClient({"status": "API_ERROR", "message": "Invalid size"}))
    state = {}
    d = em.execute(_req(), SPECS, state)
    assert d.accepted is False
    assert state.get("halt_trading") is None
    assert state.get("pending_order_id") is None

def test_live_success_clears_id():
    em = ExecutionManager("LIVE", client=MockClient({"status": "FILLED", "id": "123"}))
    state = {}
    d = em.execute(_req(), SPECS, state)
    assert d.accepted is True
    assert state.get("pending_order_id") is None