
"""Durable write-ahead order-intent journal.

This module is deliberately exchange-agnostic and never sends network requests.
A SUBMISSION_STARTED intent is ambiguous after a crash and MUST be queried at
its venue using the same client_order_id; it must never be blindly resubmitted.
The JSONL journal assumes one writer process per path. Corruption fails closed.
"""

import hashlib
import json
import os
import time
import uuid
from decimal import Decimal, InvalidOperation

SCHEMA_VERSION = 1
EVENT_TYPES = {
    "INTENT_CREATED",
    "SUBMISSION_STARTED",
    "SUBMISSION_UNKNOWN",
    "EXCHANGE_ACKNOWLEDGED",
    "RECONCILED",
}
VALID_EXCHANGE_STATUSES = {
    "PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL",
    "FILLED", "CANCELED", "REJECTED",
}
TERMINAL_EXCHANGE_STATUSES = {"FILLED", "CANCELED", "REJECTED"}


class IntentJournalError(RuntimeError):
    """Raised when the journal is invalid or a transition is unsafe."""


def _positive_decimal(value, field, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a positive number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a positive number")
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field} must be finite and > 0")
    return str(number)


def _nonempty(value, field, max_length=128):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field} must be a non-empty string <= {max_length} chars")
    return value.strip()


def _fingerprint(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_intent(intent):
    if not isinstance(intent, dict):
        raise ValueError("intent must be a dictionary")

    client_order_id = _nonempty(
        intent.get("client_order_id"), "client_order_id", 64
    )
    symbol = _nonempty(intent.get("symbol"), "symbol", 64)
    side = _nonempty(intent.get("side"), "side", 16)
    order_intent = _nonempty(intent.get("intent"), "intent", 16)
    order_type = _nonempty(intent.get("order_type"), "order_type", 16)
    signal_id = _nonempty(intent.get("signal_id"), "signal_id", 128)
    strategy_hash = _nonempty(
        intent.get("strategy_hash"), "strategy_hash", 128
    )

    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")

    if order_intent not in {"ENTRY", "EXIT", "TP1_HIT"}:
        raise ValueError("intent must be ENTRY, EXIT or TP1_HIT")

    if order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("order_type must be MARKET or LIMIT")

    qty = _positive_decimal(intent.get("qty"), "qty")
    price = _positive_decimal(
        intent.get("price"), "price", allow_none=True
    )

    if order_type == "LIMIT" and price is None:
        raise ValueError("LIMIT intent requires price")

    if order_type == "MARKET" and price is not None:
        raise ValueError("MARKET intent must not include price")

    leverage = intent.get("leverage")
    if (
        isinstance(leverage, bool)
        or not isinstance(leverage, int)
        or leverage < 1
    ):
        raise ValueError("leverage must be a positive integer")

    return {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "intent": order_intent,
        "order_type": order_type,
        "qty": qty,
        "price": price,
        "leverage": leverage,
        "signal_id": signal_id,
        "strategy_hash": strategy_hash,
    }


def _initial_record(intent, now_ms):
    normalized = _normalize_intent(intent)

    return {
        "schema_version": SCHEMA_VERSION,
        "client_order_id": normalized["client_order_id"],
        "intent": normalized,
        "fingerprint": _fingerprint(normalized),
        "status": "INTENT_CREATED",
        "exchange_order_id": None,
        "exchange_status": None,
        "reconciliation": None,
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "event_count": 1,
    }


class OrderIntentStore:
    """Append-only order intent event store with strict replay validation."""

    def __init__(self, path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("journal path is required")
        self.path = path

    def _read_events(self):
        if not os.path.exists(self.path):
            return []

        events = []

        with open(self.path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IntentJournalError(
                        f"corrupt journal JSON at line {line_no}: {exc}"
                    )

                if not isinstance(event, dict):
                    raise IntentJournalError(
                        f"journal event at line {line_no} is not an object"
                    )

                events.append(event)

        return events

    def _replay(self):
        records = {}
        seen_event_ids = set()
        expected_seq = 1

        for event in self._read_events():
            required = {
                "schema_version",
                "event_id",
                "event_seq",
                "event_type",
                "client_order_id",
                "timestamp_ms",
                "payload",
            }

            if not required.issubset(event):
                raise IntentJournalError(
                    "journal event missing required fields"
                )

            if event["schema_version"] != SCHEMA_VERSION:
                raise IntentJournalError(
                    "unsupported journal schema version"
                )

            if event["event_seq"] != expected_seq:
                raise IntentJournalError(
                    "journal event sequence is not contiguous"
                )

            expected_seq += 1

            event_id = event["event_id"]

            if (
                not isinstance(event_id, str)
                or not event_id
                or event_id in seen_event_ids
            ):
                raise IntentJournalError(
                    "invalid or duplicate journal event_id"
                )

            seen_event_ids.add(event_id)

            if event["event_type"] not in EVENT_TYPES:
                raise IntentJournalError("unknown journal event type")

            timestamp = event["timestamp_ms"]

            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp < 0
            ):
                raise IntentJournalError("invalid journal timestamp")

            if not isinstance(event["payload"], dict):
                raise IntentJournalError(
                    "journal payload must be an object"
                )

            cid = event["client_order_id"]

            if not isinstance(cid, str) or not cid:
                raise IntentJournalError(
                    "invalid client_order_id in journal"
                )

            self._apply_event(records, event)

        return records, expected_seq

    @staticmethod
    def _apply_event(records, event):
        kind = event["event_type"]
        cid = event["client_order_id"]
        payload = event["payload"]
        now_ms = event["timestamp_ms"]

        if kind == "INTENT_CREATED":
            if cid in records:
                raise IntentJournalError(
                    "duplicate INTENT_CREATED for client_order_id"
                )

            record = _initial_record(
                payload.get("intent"),
                now_ms,
            )

            if record["client_order_id"] != cid:
                raise IntentJournalError(
                    "client_order_id mismatch in create event"
                )

            if payload.get("fingerprint") != record["fingerprint"]:
                raise IntentJournalError(
                    "intent fingerprint mismatch"
                )

            records[cid] = record
            return

        if cid not in records:
            raise IntentJournalError(
                f"event references unknown intent: {cid}"
            )

        record = records[cid]
        current = record["status"]

        if kind == "SUBMISSION_STARTED":
            if current != "INTENT_CREATED":
                raise IntentJournalError(
                    f"cannot start submission from {current}"
                )

            record["status"] = kind

        elif kind == "SUBMISSION_UNKNOWN":
            if current not in {
                "SUBMISSION_STARTED",
                "SUBMISSION_UNKNOWN",
            }:
                raise IntentJournalError(
                    f"cannot mark submission unknown from {current}"
                )

            record["status"] = kind

        elif kind == "EXCHANGE_ACKNOWLEDGED":
            if current not in {
                "SUBMISSION_STARTED",
                "SUBMISSION_UNKNOWN",
                "EXCHANGE_ACKNOWLEDGED",
            }:
                raise IntentJournalError(
                    f"cannot acknowledge from {current}"
                )

            exchange_id = payload.get("exchange_order_id")
            exchange_status = payload.get("exchange_status")

            if (
                not isinstance(exchange_id, (str, int))
                or isinstance(exchange_id, bool)
                or not str(exchange_id).strip()
            ):
                raise IntentJournalError(
                    "exchange_order_id is required"
                )

            if exchange_status not in VALID_EXCHANGE_STATUSES:
                raise IntentJournalError(
                    "invalid exchange_status"
                )

            if record["exchange_order_id"] not in (
                None,
                str(exchange_id),
            ):
                raise IntentJournalError(
                    "exchange_order_id changed for same intent"
                )

            record["exchange_order_id"] = str(exchange_id)
            record["exchange_status"] = exchange_status
            record["status"] = kind

        elif kind == "RECONCILED":
            if current != "EXCHANGE_ACKNOWLEDGED":
                raise IntentJournalError(
                    f"cannot reconcile from {current}"
                )

            if record["exchange_status"] not in TERMINAL_EXCHANGE_STATUSES:
                raise IntentJournalError(
                    "cannot finalize a non-terminal exchange order"
                )

            result = payload.get("result")

            if (
                not isinstance(result, dict)
                or result.get("ok") is not True
                or result.get("halt_trading") is not False
            ):
                raise IntentJournalError(
                    "reconciliation result must be explicitly successful"
                )

            record["reconciliation"] = result
            record["status"] = kind

        record["updated_at_ms"] = now_ms
        record["event_count"] += 1

    def _append(
        self,
        event_type,
        client_order_id,
        payload,
        now_ms=None,
    ):
        records, next_seq = self._replay()

        timestamp = (
            int(time.time() * 1000)
            if now_ms is None
            else now_ms
        )

        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
        ):
            raise ValueError(
                "now_ms must be a non-negative integer"
            )

        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "event_seq": next_seq,
            "event_type": event_type,
            "client_order_id": client_order_id,
            "timestamp_ms": timestamp,
            "payload": payload,
        }

        # Validate transition before touching the file.
        self._apply_event(records, event)

        os.makedirs(
            os.path.dirname(self.path) or ".",
            exist_ok=True,
        )

        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    event,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

        return self.get(client_order_id)

    def create_intent(self, intent, now_ms=None):
        normalized = _normalize_intent(intent)
        records, _ = self._replay()

        cid = normalized["client_order_id"]
        fingerprint = _fingerprint(normalized)

        if cid in records:
            if records[cid]["fingerprint"] != fingerprint:
                raise IntentJournalError(
                    "client_order_id already exists with different intent"
                )

            return records[cid]

        for existing in records.values():
            old = existing["intent"]

            if (
                old["symbol"] == normalized["symbol"]
                and old["signal_id"] == normalized["signal_id"]
            ):
                raise IntentJournalError(
                    "duplicate signal_id already has an order intent"
                )

        return self._append(
            "INTENT_CREATED",
            cid,
            {
                "intent": normalized,
                "fingerprint": fingerprint,
            },
            now_ms,
        )

    def mark_submission_started(self, client_order_id, now_ms=None):
        return self._append(
            "SUBMISSION_STARTED",
            _nonempty(client_order_id, "client_order_id", 64),
            {},
            now_ms,
        )

    def mark_submission_unknown(
        self,
        client_order_id,
        reason,
        now_ms=None,
    ):
        reason = _nonempty(reason, "reason", 512)

        return self._append(
            "SUBMISSION_UNKNOWN",
            _nonempty(client_order_id, "client_order_id", 64),
            {"reason": reason},
            now_ms,
        )

    def acknowledge(
        self,
        client_order_id,
        exchange_order_id,
        exchange_status,
        now_ms=None,
    ):
        if (
            isinstance(exchange_order_id, bool)
            or not isinstance(exchange_order_id, (str, int))
        ):
            raise ValueError(
                "exchange_order_id must be a non-empty string or integer"
            )

        exchange_id = _nonempty(
            str(exchange_order_id),
            "exchange_order_id",
            128,
        )

        return self._append(
            "EXCHANGE_ACKNOWLEDGED",
            _nonempty(client_order_id, "client_order_id", 64),
            {
                "exchange_order_id": exchange_id,
                "exchange_status": exchange_status,
            },
            now_ms,
        )

    def mark_reconciled(
        self,
        client_order_id,
        result,
        now_ms=None,
    ):
        return self._append(
            "RECONCILED",
            _nonempty(client_order_id, "client_order_id", 64),
            {"result": result},
            now_ms,
        )

    def get(self, client_order_id):
        records, _ = self._replay()
        record = records.get(client_order_id)

        return dict(record) if record is not None else None

    def all_records(self):
        records, _ = self._replay()
        return [
            dict(record)
            for record in records.values()
        ]

    def unresolved(self):
        return [
            record
            for record in self.all_records()
            if record["status"] != "RECONCILED"
        ]

    def recovery_required(self):
        """Return unresolved records; none may be automatically resubmitted."""
        return [
            record
            for record in self.unresolved()
            if record["status"] in {
                "INTENT_CREATED",
                "SUBMISSION_STARTED",
                "SUBMISSION_UNKNOWN",
                "EXCHANGE_ACKNOWLEDGED",
            }
        ]
