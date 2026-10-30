
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

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


def _req(intent="ENTRY"):
    return OrderRequest(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        qty=1.0,
        price=None,
        leverage=3,
        intent=intent,
    )


class MockClient:
    def __init__(self):
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        raise AssertionError("LIVE network call must be blocked")

    def get_order_status(self, symbol, client_order_id):
        self.calls.append(("get_order_status", symbol, client_order_id))
        raise AssertionError("LIVE recovery query must be blocked")


def test_live_entry_blocked_before_network():
    client = MockClient()
    em = ExecutionManager("LIVE", client=client)
    state = {}

    decision = em.execute(
        _req(), SPECS, state,
        save_state_fn=lambda s: None,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert decision.reason == "real_execution_disabled_pending_reconciliation"
    assert client.calls == []
    assert state == {}


def test_demo_execution_blocked_before_network():
    client = MockClient()
    em = ExecutionManager("TOOBIT_DEMO", client=client)
    state = {}

    decision = em.execute(_req(), SPECS, state)

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert client.calls == []
    assert state == {}


def test_live_pending_fill_recovery_is_blocked():
    client = MockClient()
    em = ExecutionManager("LIVE", client=client)
    state = {
        "order_status": "PENDING_FILL",
        "pending_order_id": "apex_TEST",
    }

    decision = em.execute(_req(), SPECS, state)

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert client.calls == []
    assert state["order_status"] == "PENDING_FILL"
    assert state["pending_order_id"] == "apex_TEST"


def test_live_exit_is_also_blocked():
    client = MockClient()
    em = ExecutionManager("LIVE", client=client)
    state = {}

    decision = em.execute(
        _req(intent="EXIT"), SPECS, state,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert client.calls == []
