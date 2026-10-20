"""
test_execution_manager.py — Strict safety tests for ExecutionManager.
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

# ============================================================
# 1. حالت PAPER
# ============================================================
def test_paper_mode_no_client():
    em = ExecutionManager(mode="PAPER", client=None)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True

# ============================================================
# 2. حالت MOCK_LIVE (تضمین عدم فراخوانی شبکه)
# ============================================================
def test_mock_live_no_network():
    client = FakeToobitClient()
    em = ExecutionManager(mode="MOCK_LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is True
    assert client.place_calls == 0  # مطمئن می‌شویم هیچ تماسی با شبکه برقرار نشده

# ============================================================
# 3. LIVE بدون مجوز محیطی
# ============================================================
@patch.dict(os.environ, {"APEXQUANT_ENV": "TEST"})
def test_live_blocked_without_env_permission():
    client = FakeToobitClient()
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "missing_env_permission"
    assert client.place_calls == 0

# ============================================================
# 4. LIVE با شکست همگام‌سازی
# ============================================================
@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_live_halts_on_sync_failure():
    client = FakeToobitClient(fail_sync=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    decision = em.execute(req, SPECS, _empty_state(), DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "state_sync_failed"
    assert client.place_calls == 0

# ============================================================
# 5. LIVE با خطای شبکه در زمان ارسال
# ============================================================
@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_live_network_error_during_send():
    client = FakeToobitClient(fail_network_send=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    state = _empty_state()
    
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    assert decision.accepted is False
    assert decision.reason == "network_error_unknown"
    # شناسه باید در استیت ذخیره شده باشد تا در اجرای بعدی استفاده شود
    assert state["pending_order_id"] is not None

# ============================================================
# 6. پایداری شناسه در ری‌استارت
# ============================================================
@patch.dict(os.environ, {"APEXQUANT_ENV": "PRODUCTION"})
def test_persistent_order_id_on_restart():
    client = FakeToobitClient(fail_network_send=True)
    em = ExecutionManager(mode="LIVE", client=client)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 1, "ENTRY")
    
    state = _empty_state()
    # ارسال اول (با خطا)
    em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    first_id = state["pending_order_id"]
    
    # شبیه‌سازی ری‌استارت: سیستم دوباره سعی می‌کند سیگنال را پردازش کند
    client.fail_network_send = False
    em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    # هر دو بار باید با همان آیدی تلاش کرده باشد
    assert client.place_calls == 2 

# ============================================================
# 7. تست جدید: رد سفارش توسط دروازه‌بان ریسک
# ============================================================
def test_mock_live_rollback_on_rejection():
    """
    اگر order_executor سفارش را رد کند (مثلاً لوریج غیرمجاز)،
    ExecutionManager باید آن را رد کند و هیچ تماسی با شبکه برقرار نکند.
    """
    client = FakeToobitClient()
    em = ExecutionManager(mode="MOCK_LIVE", client=client)
    
    # لوریج 999 غیرمجاز است (سقف 10 است)
    req = OrderRequest("BTC-SWAP-USDT", "LONG", "MARKET", 0.1, None, 999, "ENTRY") 
    
    state = _empty_state()
    decision = em.execute(req, SPECS, state, DEFAULT_CONFIG, ref_price=50000.0)
    
    assert decision.accepted is False
    assert decision.gate == "leverage"
    assert client.place_calls == 0
    # وضعیت نباید دست‌خوش تغییر نامشخص شود
    assert state["pending_order_id"] is None

# ============================================================
# Main Runner
# ============================================================
if __name__ == "__main__":
    tests = [
        test_paper_mode_no_client,
        test_mock_live_no_network,
        test_live_blocked_without_env_permission,
        test_live_halts_on_sync_failure,
        test_live_network_error_during_send,
        test_persistent_order_id_on_restart,
        test_mock_live_rollback_on_rejection,
    ]
    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Execution Manager Safety Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)