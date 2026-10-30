import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from execution_manager import ExecutionManager
from order_executor import OrderRequest
from toobit_client import StandardOrderResponse

SPECS = {"contract_multiplier": 0.001, "min_qty": 0.1, "max_qty": 100.0, "qty_step": 0.1, "min_notional": 0.0, "tick_size": 0.1, "max_leverage": 10}

def _req(intent="ENTRY"):
    return OrderRequest(symbol="BTC-SWAP-USDT", side="LONG", order_type="MARKET", qty=1.0, price=None, leverage=3, intent=intent)

def _make_resp(status, filled_qty=0.0, symbol="BTC-SWAP-USDT", client_id="apex_TEST", exchange_id="EX123"):
    return StandardOrderResponse(
        exchange_order_id=exchange_id,
        client_order_id=client_id,
        symbol=symbol,
        requested_qty=1.0,
        filled_qty=filled_qty,
        avg_price=None,
        status=status,
        raw_response={},
    )

class MockClient:
    def __init__(self, resp=None, raise_exc=None, query_resp=None):
        self.resp = resp
        self.raise_exc = raise_exc
        self.query_resp = query_resp or resp

    def place_order(self, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        # Patch client_order_id to match state's generated one
        if isinstance(self.resp, StandardOrderResponse):
            self.resp.client_order_id = kwargs["client_order_id"]
        return self.resp

    def get_order_status(self, symbol, client_order_id):
        return self.query_resp

def test_unknown_blocks_all():
    em = ExecutionManager("MOCK_LIVE")
    state = {"order_status": "UNKNOWN", "position": {"side": "LONG", "remaining_size": 1.0}}
    assert em.execute(_req("ENTRY"), SPECS, state).accepted is False
    assert em.execute(_req("EXIT"), SPECS, state).accepted is False

def test_halt_allows_exit_only():
    em = ExecutionManager("MOCK_LIVE")
    state = {"halt_trading": True, "position": {"side": "LONG", "remaining_size": 1.0}}
    assert em.execute(_req("ENTRY"), SPECS, state).accepted is False
    assert em.execute(_req("EXIT"), SPECS, state).accepted is True

def test_live_client_exception_halts():
    em = ExecutionManager("LIVE", client=MockClient(raise_exc=Exception("timeout")))
    state = {}
    d = em.execute(_req("ENTRY"), SPECS, state)
    assert d.accepted is False
    assert state.get("halt_trading") is True
    assert state.get("order_status") == "UNKNOWN"

def test_live_unknown_status_halts():
    em = ExecutionManager("LIVE", client=MockClient(_make_resp("UNKNOWN")))
    state = {}
    d = em.execute(_req("ENTRY"), SPECS, state)
    assert d.accepted is False
    assert state.get("halt_trading") is True

def test_live_partially_filled_halts():
    em = ExecutionManager("LIVE", client=MockClient(_make_resp("PARTIALLY_FILLED", filled_qty=0.5)))
    state = {}
    d = em.execute(_req("ENTRY"), SPECS, state)
    assert d.accepted is False
    assert state.get("halt_trading") is True
    assert state.get("partial_fill_qty") == 0.5

def test_live_filled_valid():
    em = ExecutionManager("LIVE", client=MockClient(_make_resp("FILLED", filled_qty=1.0)))
    state = {}
    d = em.execute(_req("ENTRY"), SPECS, state)
    assert d.accepted is True
    assert state.get("order_status") == "FILLED"

def test_live_filled_qty_mismatch_halts():
    em = ExecutionManager("LIVE", client=MockClient(_make_resp("FILLED", filled_qty=0.5)))
    state = {}
    d = em.execute(_req("ENTRY"), SPECS, state)
    assert d.accepted is False
    assert state.get("halt_trading") is True

def test_live_new_halts_for_recovery():
    em = ExecutionManager("LIVE", client=MockClient(_make_resp("NEW", filled_qty=0.0)))
    state = {}
    d = em.execute(_req("ENTRY"), SPECS, state)
    assert d.accepted is False
    assert state.get("halt_trading") is True
    assert state.get("order_status") == "PENDING_FILL"