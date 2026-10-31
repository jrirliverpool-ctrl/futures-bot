
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_intent_store import (
    IntentJournalError,
    OrderIntentStore,
)


def intent(**overrides):
    value = {
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
    value.update(overrides)
    return value


def test_create_intent_is_persisted_and_idempotent(tmp_path):
    path = str(tmp_path / "orders" / "intents.jsonl")
    store = OrderIntentStore(path)

    first = store.create_intent(intent(), now_ms=1000)
    second = store.create_intent(intent(), now_ms=1001)

    assert first["status"] == "INTENT_CREATED"
    assert second["client_order_id"] == first["client_order_id"]
    assert len(store.all_records()) == 1


def test_same_client_id_with_different_payload_rejected(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))
    store.create_intent(intent(), now_ms=1000)

    with pytest.raises(IntentJournalError, match="different intent"):
        store.create_intent(intent(qty="0.02"), now_ms=1001)


def test_same_signal_cannot_create_second_client_id(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))
    store.create_intent(intent(), now_ms=1000)

    with pytest.raises(IntentJournalError, match="duplicate signal_id"):
        store.create_intent(
            intent(client_order_id="apex-test-002"),
            now_ms=1001,
        )


def test_submission_started_survives_restart_and_is_unresolved(tmp_path):
    path = str(tmp_path / "intents.jsonl")

    OrderIntentStore(path).create_intent(
        intent(),
        now_ms=1000,
    )
    OrderIntentStore(path).mark_submission_started(
        "apex-test-001",
        now_ms=1001,
    )

    recovered = OrderIntentStore(path).recovery_required()

    assert len(recovered) == 1
    assert recovered[0]["status"] == "SUBMISSION_STARTED"
    assert recovered[0]["client_order_id"] == "apex-test-001"


def test_ambiguous_submission_never_becomes_resubmittable(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))

    store.create_intent(intent(), now_ms=1000)
    store.mark_submission_started("apex-test-001", now_ms=1001)
    store.mark_submission_unknown(
        "apex-test-001",
        "timeout",
        now_ms=1002,
    )

    assert store.recovery_required()[0]["status"] == "SUBMISSION_UNKNOWN"

    with pytest.raises(IntentJournalError, match="cannot start submission"):
        store.mark_submission_started(
            "apex-test-001",
            now_ms=1003,
        )


def test_acknowledgement_can_follow_unknown_recovery(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))

    store.create_intent(intent(), now_ms=1000)
    store.mark_submission_started("apex-test-001", now_ms=1001)
    store.mark_submission_unknown(
        "apex-test-001",
        "timeout",
        now_ms=1002,
    )

    record = store.acknowledge(
        "apex-test-001",
        "exchange-77",
        "PARTIALLY_FILLED",
        now_ms=1003,
    )

    assert record["status"] == "EXCHANGE_ACKNOWLEDGED"
    assert record["exchange_order_id"] == "exchange-77"


def test_exchange_order_id_cannot_change(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))

    store.create_intent(intent(), now_ms=1000)
    store.mark_submission_started("apex-test-001", now_ms=1001)
    store.acknowledge(
        "apex-test-001",
        "exchange-77",
        "NEW",
        now_ms=1002,
    )

    with pytest.raises(IntentJournalError, match="exchange_order_id changed"):
        store.acknowledge(
            "apex-test-001",
            "exchange-88",
            "NEW",
            now_ms=1003,
        )


def test_nonterminal_order_cannot_be_finalized(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))

    store.create_intent(intent(), now_ms=1000)
    store.mark_submission_started("apex-test-001", now_ms=1001)
    store.acknowledge(
        "apex-test-001",
        "exchange-77",
        "PARTIALLY_FILLED",
        now_ms=1002,
    )

    with pytest.raises(IntentJournalError, match="non-terminal"):
        store.mark_reconciled(
            "apex-test-001",
            {"ok": True, "halt_trading": False},
            now_ms=1003,
        )


def test_only_explicit_successful_reconciliation_finalizes(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))

    store.create_intent(intent(), now_ms=1000)
    store.mark_submission_started("apex-test-001", now_ms=1001)
    store.acknowledge(
        "apex-test-001",
        "exchange-77",
        "FILLED",
        now_ms=1002,
    )

    with pytest.raises(IntentJournalError, match="explicitly successful"):
        store.mark_reconciled(
            "apex-test-001",
            {"ok": False, "halt_trading": True},
            now_ms=1003,
        )

    record = store.mark_reconciled(
        "apex-test-001",
        {"ok": True, "halt_trading": False, "fill_count": 2},
        now_ms=1004,
    )

    assert record["status"] == "RECONCILED"
    assert store.unresolved() == []


def test_invalid_intent_rejected(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))

    with pytest.raises(ValueError):
        store.create_intent(intent(qty="NaN"), now_ms=1000)

    with pytest.raises(ValueError):
        store.create_intent(
            intent(order_type="LIMIT", price=None),
            now_ms=1000,
        )

    with pytest.raises(ValueError):
        store.create_intent(intent(leverage=True), now_ms=1000)


def test_corrupt_journal_fails_closed(tmp_path):
    path = tmp_path / "intents.jsonl"
    path.write_text('{"bad":\n', encoding="utf-8")

    with pytest.raises(IntentJournalError, match="corrupt journal"):
        OrderIntentStore(str(path)).all_records()


def test_unknown_event_or_bad_sequence_fails_closed(tmp_path):
    path = tmp_path / "intents.jsonl"

    path.write_text(
        '{"schema_version":1,"event_id":"x","event_seq":2,'
        '"event_type":"NOPE","client_order_id":"x",'
        '"timestamp_ms":1,"payload":{}}\n',
        encoding="utf-8",
    )

    with pytest.raises(IntentJournalError):
        OrderIntentStore(str(path)).all_records()


def test_no_network_capability_in_store(tmp_path):
    store = OrderIntentStore(str(tmp_path / "intents.jsonl"))
    record = store.create_intent(intent(), now_ms=1000)

    assert "exchange_order_id" in record
    assert not hasattr(store, "place_order")
    assert not hasattr(store, "submit")
