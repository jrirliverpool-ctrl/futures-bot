import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_executor import OrderRequest
from order_intent_store import OrderIntentStore
from toobit_execution_bridge import (
    BridgeSafetyError,
    ToobitExecutionBridge,
)


SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.1,
    "max_qty": 100.0,
    "qty_step": 0.1,
    "min_notional": 1.0,
    "min_notional_verified": True,
    "tick_size": 0.1,
    "max_leverage": 10,
}

CONFIG = {
    "max_concurrent_positions": 1,
    "leverage_cap": 3,
}

STRATEGY_HASH = "sha256:" + "a" * 64


def make_request(**overrides):
    values = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "order_type": "MARKET",
        "qty": 1.0,
        "price": None,
        "leverage": 3,
        "intent": "ENTRY",
    }
    values.update(overrides)
    return OrderRequest(**values)


def make_state(**overrides):
    state = {
        "position": None,
        "pending_entry": None,
        "pending_order_id": None,
        "halt_trading": False,
        "kill_switch": False,
        "circuit_breaker_active": False,
    }
    state.update(overrides)
    return state


@pytest.fixture
def bridge(tmp_path):
    store = OrderIntentStore(
        str(tmp_path / "state" / "order_intents.jsonl")
    )
    return ToobitExecutionBridge(store)


def test_valid_order_is_journaled_but_not_submitted(bridge):
    result = bridge.prepare_order(
        req=make_request(),
        specs=SPECS,
        state=make_state(),
        signal_id="BTC-1h-1790000000000-ENTRY",
        strategy_hash=STRATEGY_HASH,
        config=CONFIG,
        ref_price=67000.0,
        now_ms=1000,
    )

    assert result.accepted is True
    assert result.stage == "INTENT_RECORDED_NOT_SUBMITTED"
    assert result.client_order_id
    assert result.journal_status == "INTENT_CREATED"


def test_invalid_minimum_notional_blocks_intent(bridge):
    bad_specs = dict(SPECS)
    bad_specs["min_notional_verified"] = False

    result = bridge.prepare_order(
        req=make_request(),
        specs=bad_specs,
        state=make_state(),
        signal_id="BTC-1h-1790000000001-ENTRY",
        strategy_hash=STRATEGY_HASH,
        config=CONFIG,
        ref_price=67000.0,
        now_ms=1000,
    )

    assert result.accepted is False
    assert result.gate == "notional"
    assert result.client_order_id is None


def test_unresolved_intent_blocks_new_preparation(bridge):
    first = bridge.prepare_order(
        req=make_request(),
        specs=SPECS,
        state=make_state(),
        signal_id="BTC-1h-1790000000002-ENTRY",
        strategy_hash=STRATEGY_HASH,
        config=CONFIG,
        ref_price=67000.0,
        now_ms=1000,
    )

    assert first.accepted is True

    second = bridge.prepare_order(
        req=make_request(symbol="ETH-SWAP-USDT"),
        specs=SPECS,
        state=make_state(),
        signal_id="ETH-1h-1790000000002-ENTRY",
        strategy_hash=STRATEGY_HASH,
        config=CONFIG,
        ref_price=3000.0,
        now_ms=1001,
    )

    assert second.accepted is False
    assert second.reason == "unresolved_order_intent_recovery_required"


def test_kill_switch_blocks_order_before_journaling(bridge):
    result = bridge.prepare_order(
        req=make_request(),
        specs=SPECS,
        state=make_state(kill_switch=True),
        signal_id="BTC-1h-1790000000003-ENTRY",
        strategy_hash=STRATEGY_HASH,
        config=CONFIG,
        ref_price=67000.0,
        now_ms=1000,
    )

    assert result.accepted is False
    assert result.gate == "kill_switch"


def test_deterministic_client_order_id(bridge):
    request = make_request()

    first = bridge._make_client_order_id(
        request,
        "BTC-1h-1790000000004-ENTRY",
        STRATEGY_HASH,
    )

    second = bridge._make_client_order_id(
        request,
        "BTC-1h-1790000000004-ENTRY",
        STRATEGY_HASH,
    )

    assert first == second
    assert first.startswith("aq")
    assert len(first) <= 64


def test_submit_order_is_explicitly_disabled(bridge):
    with pytest.raises(
        BridgeSafetyError,
        match="exchange_submission_disabled",
    ):
        bridge.submit_order()


def test_market_request_with_price_is_rejected(bridge):
    result = bridge.prepare_order(
        req=make_request(price=67000.0),
        specs=SPECS,
        state=make_state(),
        signal_id="BTC-1h-1790000000005-ENTRY",
        strategy_hash=STRATEGY_HASH,
        config=CONFIG,
        ref_price=67000.0,
        now_ms=1000,
    )

    assert result.accepted is False
    assert result.gate == "price"