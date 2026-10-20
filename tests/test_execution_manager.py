"""
test_execution_manager.py — Strict safety tests using pytest.
Run with: pytest -q tests/test_execution_manager.py
"""
import os
import sys
import json
from unittest.mock import patch
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_executor import OrderRequest, DEFAULT_CONFIG
from execution_manager import ExecutionManager

SPECS = {
    "min_qty": 0.01, "max_qty": 100.0, "qty_step": 0.01,
    "tick_size": 0.1, "min_notional": 10.0,
    "contract_multiplier": 1.0, "max_leverage": 10
}

def _empty_state():
    return {
        "position": None, "pending_entry": None,
        "circuit_breaker_active": False, "kill_switch": False,
        "pending_order_id": None, "order_status": None
    }

class FakeToobitClient:
    def __init__(self, fail_sync=False, fail_network_send=False, invalid_sync_response=False):
        self.fail_sync = fail_sync
        self.fail_network_send = fail_network_send
        self.invalid_sync_response = invalid_sync_response
        self.place_calls = []
        self.place_ids = []

    def get_position(self, symbol):
        if self.invalid_sync_response:
            return None  # پاسخ نامعتبر
        if self.fail_sync:
            return {"error": "Timeout"}
        return {"position": None}

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        self.place_ids.append(kwargs.get("client_order_id"))
        if self.fail_network_send:
            return {"status": "NETWORK_ERROR"}
        return {"orderId": "123", "status": "SUCCESS"}


def test_paper_mode_no_client():
    em = ExecutionManager(mode="PAPER", client=None)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True


def test_mock_live_no_network():
    client = FakeToobitClient()
    em = ExecutionManager(mode="MOCK_LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True
    assert len(client.place_calls) == 0


@patch.dict(os.environ, {"APEXQUANT_ENV": "TEST"})
def test_live_blocked_without_env_permission():
    client = FakeToobitClient()
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "missing_env_permission"
    assert len(client.place_calls) == 0


@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_live_halts_on_sync_failure():
    client = FakeToobitClient(fail_sync=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "state_sync_failed"
    assert len(client.place_calls) == 0


@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_live_halts_on_invalid_sync_response():
    client = FakeToobitClient(invalid_sync_response=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "state_sync_failed"
    assert len(client.place_calls) == 0


@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_live_network_error_during_send():
    client = FakeToobitClient(fail_network_send=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    state = _empty_state()
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    
    assert decision.accepted is False
    assert decision.reason == "network_error_unknown"
    assert state["pending_order_id"] is not None
    # ✅ تایید اینکه وضعیت مبهم ثبت شده است
    assert state["order_status"] == "UNKNOWN"


def test_ambiguous_state_blocks_next_order():
    em = ExecutionManager(mode="MOCK_LIVE", client=None)
    state = _empty_state()
    state["order_status"] = "UNKNOWN" # شبیه‌سازی وضعیت مبهم
    
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    
    assert decision.accepted is False
    assert decision.reason == "ambiguous_order_halt"


@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_persistent_order_id_on_restart():
    client = FakeToobitClient(fail_network_send=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    state = _empty_state()
    
    # اجرای اول (خطای شبکه)
    em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    first_id = state["pending_order_id"]
    
    # ✅ شبیه‌سازی ری‌استارت: ذخیره در فایل و بازیابی
    serialized_state = json.dumps(state)
    restored_state = json.loads(serialized_state)
    
    assert restored_state["pending_order_id"] == first_id
    
    # حالا اگر سیستم دوباره اجرا شود، شناسه باید حفظ شده باشد
    client.fail_network_send = False
    state["order_status"] = None # آزادسازی برای تست
    em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    
    assert len(client.place_ids) == 2
    assert client.place_ids[0] == first_id
    assert client.place_ids[1] == first_id


def test_mock_live_rollback_on_rejection():
    client = FakeToobitClient()
    em = ExecutionManager(mode="MOCK_LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 999, "ENTRY") # لوریج غیرمجاز
    state = _empty_state()
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.gate == "leverage"
    assert len(client.place_calls) == 0
    assert state["pending_order_id"] is None


def test_kill_switch_blocks_entry():
    em = ExecutionManager(mode="MOCK_LIVE", client=None)
    state = _empty_state()
    state["kill_switch"] = True
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.gate == "safety"


def test_kill_switch_allows_exit():
    em = ExecutionManager(mode="MOCK_LIVE", client=None)
    state = _empty_state()
    state["kill_switch"] = True
    req = OrderRequest("BTC-SWAP-USDT", "SHORT", "MARKET", 0.1, None, 1, "EXIT")
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True