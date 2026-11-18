
"""Mock-only order lifecycle transitions for ApexQuant.

This module exercises the durable intent journal without importing an exchange
client, HTTP library, credentials, or the Toobit order adapter. It is a test
harness, NOT a paper fill simulator and NOT a live execution path.

A SUBMISSION_STARTED or SUBMISSION_UNKNOWN record is never resubmitted here.
An exchange acknowledgement may be attached during explicit recovery, but the
caller must obtain that evidence out-of-band. Only terminal exchange statuses
can be reconciled in the underlying journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from order_intent_store import (
    IntentJournalError,
    OrderIntentStore,
    VALID_EXCHANGE_STATUSES,
    TERMINAL_EXCHANGE_STATUSES,
)


class MockLifecycleError(RuntimeError):
    """Raised when a mock lifecycle transition is invalid or unsafe."""


@dataclass(frozen=True)
class MockLifecycleResult:
    mode: str
    client_order_id: str
    status: str
    exchange_order_id: str | None
    exchange_status: str | None
    unresolved: bool


class MockOrderLifecycle:
    """Explicit journal-state exerciser with no network capability."""

    MODE = "MOCK_ONLY"

    def __init__(self, intent_store: OrderIntentStore):
        if intent_store is None:
            raise ValueError("intent_store is required")

        required = (
            "get",
            "mark_submission_started",
            "mark_submission_unknown",
            "acknowledge",
            "mark_reconciled",
            "recovery_required",
        )

        for name in required:
            if not callable(getattr(intent_store, name, None)):
                raise ValueError(f"intent_store is missing {name}")

        self._store = intent_store

    @staticmethod
    def _client_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 64
        ):
            raise ValueError(
                "client_order_id must be non-empty and <= 64 chars"
            )

        return value.strip()

    def _record(self, client_order_id: str) -> dict:
        cid = self._client_id(client_order_id)

        try:
            record = self._store.get(cid)
        except Exception as exc:
            raise MockLifecycleError(
                "intent_journal_unreadable"
            ) from exc

        if not isinstance(record, dict):
            raise MockLifecycleError("intent_not_found")

        return record

    @staticmethod
    def _result(record: dict) -> MockLifecycleResult:
        status = record.get("status")

        return MockLifecycleResult(
            mode=MockOrderLifecycle.MODE,
            client_order_id=record["client_order_id"],
            status=status,
            exchange_order_id=record.get("exchange_order_id"),
            exchange_status=record.get("exchange_status"),
            unresolved=status != "RECONCILED",
        )

    def begin_mock_submission(
        self,
        client_order_id: str,
        *,
        now_ms: int | None = None,
    ) -> MockLifecycleResult:
        """Record the one permitted submission-start transition; send nothing."""

        record = self._record(client_order_id)

        if record.get("status") != "INTENT_CREATED":
            raise MockLifecycleError(
                "mock_submission_requires_INTENT_CREATED"
            )

        try:
            unresolved = self._store.recovery_required()
        except Exception as exc:
            raise MockLifecycleError(
                "intent_recovery_unreadable"
            ) from exc

        if not isinstance(unresolved, list):
            raise MockLifecycleError(
                "intent_recovery_result_invalid"
            )

        if any(
            item.get("client_order_id") != record["client_order_id"]
            for item in unresolved
            if isinstance(item, dict)
        ) or any(not isinstance(item, dict) for item in unresolved):
            raise MockLifecycleError(
                "another_unresolved_intent_blocks_mock_submission"
            )

        try:
            updated = self._store.mark_submission_started(
                record["client_order_id"],
                now_ms=now_ms,
            )
        except (IntentJournalError, OSError, ValueError) as exc:
            raise MockLifecycleError(
                "mock_submission_start_rejected"
            ) from exc

        return self._result(updated)

    def mark_mock_outcome_unknown(
        self,
        client_order_id: str,
        reason: str,
        *,
        now_ms: int | None = None,
    ) -> MockLifecycleResult:
        """Record ambiguity; the lifecycle will not retry or resubmit."""

        record = self._record(client_order_id)

        if record.get("status") not in {
            "SUBMISSION_STARTED",
            "SUBMISSION_UNKNOWN",
        }:
            raise MockLifecycleError(
                "unknown_outcome_requires_started_submission"
            )

        try:
            updated = self._store.mark_submission_unknown(
                record["client_order_id"],
                reason,
                now_ms=now_ms,
            )
        except (IntentJournalError, OSError, ValueError) as exc:
            raise MockLifecycleError(
                "mock_unknown_transition_rejected"
            ) from exc

        return self._result(updated)

    def record_recovered_acknowledgement(
        self,
        client_order_id: str,
        exchange_order_id: str | int,
        exchange_status: str,
        *,
        now_ms: int | None = None,
    ) -> MockLifecycleResult:
        """Attach externally verified acknowledgement; never queries an exchange."""

        if exchange_status not in VALID_EXCHANGE_STATUSES:
            raise ValueError("invalid exchange_status")

        record = self._record(client_order_id)

        if record.get("status") not in {
            "SUBMISSION_STARTED",
            "SUBMISSION_UNKNOWN",
            "EXCHANGE_ACKNOWLEDGED",
        }:
            raise MockLifecycleError(
                "acknowledgement_not_allowed_from_current_state"
            )

        try:
            updated = self._store.acknowledge(
                record["client_order_id"],
                exchange_order_id,
                exchange_status,
                now_ms=now_ms,
            )
        except (IntentJournalError, OSError, ValueError) as exc:
            raise MockLifecycleError(
                "mock_acknowledgement_rejected"
            ) from exc

        return self._result(updated)

    def reconcile_terminal_mock_record(
        self,
        client_order_id: str,
        reconciliation: dict,
        *,
        now_ms: int | None = None,
    ) -> MockLifecycleResult:
        """Exercise terminal journal finalization with an explicitly mock proof."""

        record = self._record(client_order_id)

        if record.get("status") != "EXCHANGE_ACKNOWLEDGED":
            raise MockLifecycleError(
                "reconciliation_requires_exchange_acknowledgement"
            )

        if record.get("exchange_status") not in TERMINAL_EXCHANGE_STATUSES:
            raise MockLifecycleError(
                "reconciliation_requires_terminal_status"
            )

        if not isinstance(reconciliation, dict):
            raise ValueError("reconciliation must be a dictionary")

        if reconciliation.get("evidence_mode") != "MOCK_ONLY":
            raise ValueError(
                "reconciliation evidence_mode must be MOCK_ONLY"
            )

        if (
            reconciliation.get("ok") is not True
            or reconciliation.get("halt_trading") is not False
        ):
            raise ValueError(
                "mock reconciliation must explicitly pass"
            )

        try:
            updated = self._store.mark_reconciled(
                record["client_order_id"],
                reconciliation,
                now_ms=now_ms,
            )
        except (IntentJournalError, OSError, ValueError) as exc:
            raise MockLifecycleError(
                "mock_reconciliation_rejected"
            ) from exc

        return self._result(updated)

    def recovery_snapshot(self) -> dict:
        """Return a read-only summary; unresolved records remain blocked."""

        try:
            unresolved = self._store.recovery_required()
        except Exception as exc:
            raise MockLifecycleError(
                "intent_recovery_unreadable"
            ) from exc

        if not isinstance(unresolved, list):
            raise MockLifecycleError(
                "intent_recovery_result_invalid"
            )

        return {
            "mode": self.MODE,
            "unresolved_count": len(unresolved),
            "unresolved_client_order_ids": [
                item.get("client_order_id")
                for item in unresolved
            ],
            "new_submission_allowed": len(unresolved) == 0,
            "network_requests": 0,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "is_live_execution": False,
        }
