
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from execution_manager import ExecutionManager
from order_executor import OrderRequest
from order_intent_store import (
    IntentJournalError,
    OrderIntentStore,
)


# Test-only specifications.
# These values are synthetic and must never be used for live exchange trading.
SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.1,
    "max_qty": 100.0,
    "qty_step": 0.1,
    "min_notional": 0.01,
    "min_notional_verified": True,
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


def _intent():
    return {
        "client_order_id": "apex-test-001",
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "intent": "ENTRY",
        "order_type": "MARKET",
        "qty": "0.01",
        "price": None,
        "leverage": 3,
        "signal_id": "BTC-1h-1790000000000-ENTRY",
        "strategy_hash": "sha256:" + "a" * 64,
    }


class MockClient:
    def __init__(self):
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        raise AssertionError("LIVE network call must be blocked")

    def get_order_status(self, symbol, client_order_id):
        self.calls.append(
            ("get_order_status", symbol, client_order_id)
        )
        raise AssertionError("LIVE recovery query must be blocked")


class ExplodingIntentStore:
    def recovery_required(self):
        raise AssertionError(
            "Real-mode safety lock must run before journal access"
        )


def test_live_entry_blocked_before_network(tmp_path):
    client = MockClient()

    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    em = ExecutionManager(
        "LIVE",
        client=client,
        intent_store=store,
    )

    state = {}

    decision = em.execute(
        _req(),
        SPECS,
        state,
        save_state_fn=lambda s: None,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert decision.reason == (
        "real_execution_disabled_pending_reconciliation"
    )

    assert client.calls == []
    assert state == {}


def test_demo_execution_blocked_before_network(tmp_path):
    client = MockClient()

    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    em = ExecutionManager(
        "TOOBIT_DEMO",
        client=client,
        intent_store=store,
    )

    state = {}

    decision = em.execute(
        _req(),
        SPECS,
        state,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"

    assert client.calls == []
    assert state == {}


def test_live_pending_fill_recovery_is_blocked(tmp_path):
    client = MockClient()

    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    em = ExecutionManager(
        "LIVE",
        client=client,
        intent_store=store,
    )

    state = {
        "order_status": "PENDING_FILL",
        "pending_order_id": "apex_TEST",
    }

    decision = em.execute(
        _req(),
        SPECS,
        state,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"

    assert client.calls == []

    assert state["order_status"] == "PENDING_FILL"
    assert state["pending_order_id"] == "apex_TEST"


def test_live_exit_is_also_blocked(tmp_path):
    client = MockClient()

    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    em = ExecutionManager(
        "LIVE",
        client=client,
        intent_store=store,
    )

    state = {}

    decision = em.execute(
        _req(intent="EXIT"),
        SPECS,
        state,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"

    assert client.calls == []


def test_mock_live_allows_execution_with_empty_journal(tmp_path):
    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    em = ExecutionManager(
        "MOCK_LIVE",
        intent_store=store,
    )

    decision = em.execute(
        _req(),
        SPECS,
        {},
        ref_price=100000.0,
    )

    assert decision.accepted is True
    assert decision.reason is None


def test_mock_live_blocks_when_intent_is_unresolved(tmp_path):
    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    store.create_intent(
        _intent(),
        now_ms=1000,
    )

    store.mark_submission_started(
        "apex-test-001",
        now_ms=1001,
    )

    em = ExecutionManager(
        "MOCK_LIVE",
        intent_store=store,
    )

    state = {}

    decision = em.execute(
        _req(),
        SPECS,
        state,
        ref_price=100000.0,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert decision.reason == (
        "unresolved_order_intent_recovery_required"
    )

    assert state == {}


def test_paper_blocks_when_submission_outcome_is_unknown(tmp_path):
    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    store.create_intent(
        _intent(),
        now_ms=1000,
    )

    store.mark_submission_started(
        "apex-test-001",
        now_ms=1001,
    )

    store.mark_submission_unknown(
        "apex-test-001",
        "timeout",
        now_ms=1002,
    )

    em = ExecutionManager(
        "PAPER",
        intent_store=store,
    )

    decision = em.execute(
        _req(),
        SPECS,
        {},
        ref_price=100000.0,
    )

    assert decision.accepted is False
    assert decision.reason == (
        "unresolved_order_intent_recovery_required"
    )


def test_corrupt_intent_journal_fails_closed(tmp_path):
    path = tmp_path / "intents.jsonl"

    path.write_text(
        '{"invalid":\n',
        encoding="utf-8",
    )

    store = OrderIntentStore(str(path))

    em = ExecutionManager(
        "MOCK_LIVE",
        intent_store=store,
    )

    state = {}

    decision = em.execute(
        _req(),
        SPECS,
        state,
        ref_price=100000.0,
    )

    assert decision.accepted is False
    assert decision.gate == "safety"
    assert decision.reason == (
        "order_intent_journal_unreadable"
    )

    assert state == {}


def test_live_hard_block_runs_before_journal_access():
    em = ExecutionManager(
        "LIVE",
        intent_store=ExplodingIntentStore(),
    )

    decision = em.execute(
        _req(),
        SPECS,
        {},
    )

    assert decision.accepted is False
    assert decision.reason == (
        "real_execution_disabled_pending_reconciliation"
    )


def test_unresolved_intent_does_not_get_resubmitted(tmp_path):
    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    store.create_intent(
        _intent(),
        now_ms=1000,
    )

    store.mark_submission_started(
        "apex-test-001",
        now_ms=1001,
    )

    em = ExecutionManager(
        "MOCK_LIVE",
        intent_store=store,
    )

    first = em.execute(
        _req(),
        SPECS,
        {},
        ref_price=100000.0,
    )

    second = em.execute(
        _req(),
        SPECS,
        {},
        ref_price=100000.0,
    )

    assert first.accepted is False
    assert second.accepted is False

    assert len(store.all_records()) == 1
    assert store.get("apex-test-001")["status"] == (
        "SUBMISSION_STARTED"
    )


def test_journal_failure_does_not_mutate_trading_state(tmp_path):
    path = tmp_path / "intents.jsonl"

    path.write_text(
        "not-json\n",
        encoding="utf-8",
    )

    store = OrderIntentStore(str(path))

    em = ExecutionManager(
        "MOCK_LIVE",
        intent_store=store,
    )

    state = {
        "equity": 1000.0,
        "position": None,
        "halt_trading": False,
    }

    before = dict(state)

    decision = em.execute(
        _req(),
        SPECS,
        state,
        ref_price=100000.0,
    )

    assert decision.accepted is False
    assert state == before
