"""
test_execution_manager.py — Strict safety tests.
"""
import os
import sys
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
        "pending_order_id": None
    }

class FakeToobitClient:
    def __init__(self, fail_sync=False, fail_network_send=False):
        self.fail_sync = fail_sync
        self.fail_network_send = fail_network_send
        self.place_calls = 0

    def get_position(self, symbol):
        if self.fail_sync:
            return {"error": "Timeout"}
        return {"position": None}

    def place_order(self, **kwargs):
        self.place_calls += 1
        if self.fail_network_send:
            return {"status": "NETWORK_ERROR"}
        return {"orderId": "123", "status": "SUCCESS"}

def test_paper_mode_no_client():
    em = ExecutionManager(mode="PAPER", client=None)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True

def test_mock_live_no_network():
    # این تست ثابت می‌کند در حالت MOCK_LIVE، حتی اگر کلاینت وجود داشته باشد، فراخوانی نمی‌شود
    client = FakeToobitClient()
    em = ExecutionManager(mode="MOCK_LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True
    assert client.place_calls == 0 # مطمئن می‌شویم هیچ تماسی با شبکه برقرار نشده

@patch.dict(os.environ, {"APEXQUANT_ENV": "TEST"})
def test_live_blocked_without_env_permission():
    client = FakeToobitClient()
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "missing_env_permission"
    assert client.place_calls == 0

if __name__ == "__main__":
    tests = [
        test_paper_mode_no_client,
        test_mock_live_no_network,
        test_live_blocked_without_env_permission,
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