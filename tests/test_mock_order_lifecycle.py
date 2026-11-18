
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

from mock_order_lifecycle import (
    MockLifecycleError,
    MockOrderLifecycle,
)
from order_intent_store import OrderIntentStore


def setup(tmp_path):
    store = OrderIntentStore(
        str(tmp_path / "intents.jsonl")
    )

    store.create_intent(
        {
            "client_order_id": "aq-mock-001",
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "intent": "ENTRY",
            "order_type": "MARKET",
            "qty": "0.1",
            "price": None,
            "leverage": 1,
            "signal_id": "btc-test-001",
            "strategy_hash": "sha256:" + "a" * 64,
        },
        now_ms=1000,
    )

    return store, MockOrderLifecycle(store)


def test_complete_mock_terminal_flow(tmp_path):
    store, flow = setup(tmp_path)

    started = flow.begin_mock_submission(
        "aq-mock-001",
        now_ms=1001,
    )

    assert started.status == "SUBMISSION_STARTED"
    assert started.mode == "MOCK_ONLY"
    assert started.unresolved is True

    partial = flow.record_recovered_acknowledgement(
        "aq-mock-001",
        "mock-77",
        "PARTIALLY_FILLED",
        now_ms=1002,
    )

    assert partial.status == "EXCHANGE_ACKNOWLEDGED"
    assert partial.exchange_status == "PARTIALLY_FILLED"
    assert partial.unresolved is True

    filled = flow.record_recovered_acknowledgement(
        "aq-mock-001",
        "mock-77",
        "FILLED",
        now_ms=1003,
    )

    assert filled.exchange_status == "FILLED"

    done = flow.reconcile_terminal_mock_record(
        "aq-mock-001",
        {
            "evidence_mode": "MOCK_ONLY",
            "ok": True,
            "halt_trading": False,
        },
        now_ms=1004,
    )

    assert done.status == "RECONCILED"
    assert done.unresolved is False
    assert store.recovery_required() == []


def test_unknown_never_resubmits(tmp_path):
    _, flow = setup(tmp_path)

    flow.begin_mock_submission(
        "aq-mock-001",
        now_ms=1001,
    )

    flow.mark_mock_outcome_unknown(
        "aq-mock-001",
        "timeout",
        now_ms=1002,
    )

    with pytest.raises(MockLifecycleError):
        flow.begin_mock_submission(
            "aq-mock-001",
            now_ms=1003,
        )

    snapshot = flow.recovery_snapshot()

    assert snapshot["unresolved_count"] == 1
    assert snapshot["new_submission_allowed"] is False
    assert snapshot["network_requests"] == 0


def test_unknown_can_be_resolved_by_recovered_ack(tmp_path):
    _, flow = setup(tmp_path)

    flow.begin_mock_submission(
        "aq-mock-001",
        now_ms=1001,
    )

    flow.mark_mock_outcome_unknown(
        "aq-mock-001",
        "timeout",
        now_ms=1002,
    )

    flow.record_recovered_acknowledgement(
        "aq-mock-001",
        "mock-77",
        "CANCELED",
        now_ms=1003,
    )

    done = flow.reconcile_terminal_mock_record(
        "aq-mock-001",
        {
            "evidence_mode": "MOCK_ONLY",
            "ok": True,
            "halt_trading": False,
        },
        now_ms=1004,
    )

    assert done.status == "RECONCILED"


def test_partial_fill_is_not_terminal(tmp_path):
    _, flow = setup(tmp_path)

    flow.begin_mock_submission(
        "aq-mock-001",
        now_ms=1001,
    )

    flow.record_recovered_acknowledgement(
        "aq-mock-001",
        "mock-77",
        "PARTIALLY_FILLED",
        now_ms=1002,
    )

    with pytest.raises(
        MockLifecycleError,
        match="terminal_status",
    ):
        flow.reconcile_terminal_mock_record(
            "aq-mock-001",
            {
                "evidence_mode": "MOCK_ONLY",
                "ok": True,
                "halt_trading": False,
            },
            now_ms=1003,
        )


def test_reconciliation_requires_mock_evidence_and_success(tmp_path):
    _, flow = setup(tmp_path)

    flow.begin_mock_submission(
        "aq-mock-001",
        now_ms=1001,
    )

    flow.record_recovered_acknowledgement(
        "aq-mock-001",
        "mock-77",
        "FILLED",
        now_ms=1002,
    )

    with pytest.raises(ValueError, match="MOCK_ONLY"):
        flow.reconcile_terminal_mock_record(
            "aq-mock-001",
            {
                "evidence_mode": "LIVE",
                "ok": True,
                "halt_trading": False,
            },
            now_ms=1003,
        )

    with pytest.raises(ValueError, match="explicitly pass"):
        flow.reconcile_terminal_mock_record(
            "aq-mock-001",
            {
                "evidence_mode": "MOCK_ONLY",
                "ok": False,
                "halt_trading": True,
            },
            now_ms=1003,
        )


def test_second_start_rejected(tmp_path):
    _, flow = setup(tmp_path)

    flow.begin_mock_submission(
        "aq-mock-001",
        now_ms=1001,
    )

    with pytest.raises(MockLifecycleError):
        flow.begin_mock_submission(
            "aq-mock-001",
            now_ms=1002,
        )


def test_no_exchange_network_methods(tmp_path):
    _, flow = setup(tmp_path)

    assert not hasattr(flow, "submit_order")
    assert not hasattr(flow, "place_order")
    assert not hasattr(flow, "client")

    result = flow.recovery_snapshot()

    assert result["network_requests"] == 0
    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0
    assert result["is_live_execution"] is False
