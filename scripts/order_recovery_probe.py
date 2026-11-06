
"""Read-only exchange probe for unresolved Toobit order intents.

Supports:
- Legacy V1 combined trade sides.
- V2 side + positionSide responses.
- Read-only order status and user-trade queries.
- Fail-closed reconciliation for incomplete or ambiguous results.

This module NEVER places, amends, or cancels orders.
It NEVER mutates the intent journal, fill ledger, or trading state.

A successful probe is evidence for manual reconciliation only.
Trading remains halted while unresolved journal records exist.
"""

import time
from decimal import Decimal, InvalidOperation

from fill_ledger import reconcile_order_fills


TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED"}

PROBE_STATUSES = {
    "SUBMISSION_STARTED",
    "SUBMISSION_UNKNOWN",
    "EXCHANGE_ACKNOWLEDGED",
}

SIDE_MAP = {
    ("LONG", "ENTRY"): "BUY_OPEN",
    ("LONG", "EXIT"): "SELL_CLOSE",
    ("LONG", "TP1_HIT"): "SELL_CLOSE",
    ("SHORT", "ENTRY"): "SELL_OPEN",
    ("SHORT", "EXIT"): "BUY_CLOSE",
    ("SHORT", "TP1_HIT"): "BUY_CLOSE",
}

V2_SIDE_MAP = {
    ("BUY", "LONG"): "BUY_OPEN",
    ("SELL", "LONG"): "SELL_CLOSE",
    ("SELL", "SHORT"): "SELL_OPEN",
    ("BUY", "SHORT"): "BUY_CLOSE",
}

LEGACY_TRADE_SIDES = {
    "BUY_OPEN",
    "SELL_OPEN",
    "BUY_CLOSE",
    "SELL_CLOSE",
}


def _decimal(value, field):
    """Parse a finite decimal without accepting booleans or nulls."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} missing or invalid")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} is not numeric") from None

    if not result.is_finite():
        raise ValueError(f"{field} is not finite")

    return result


def _canonical_side(side, position_side=None):
    """Convert V2 side fields or legacy combined side into one format."""
    if side in LEGACY_TRADE_SIDES:
        return side

    if position_side is not None:
        mapped = V2_SIDE_MAP.get((side, position_side))
        if mapped is not None:
            return mapped

    raise ValueError("invalid exchange side fields")


def _raw_order(response):
    """Extract and validate an order snapshot from the client response."""
    raw = getattr(response, "raw_response", None)

    if not isinstance(raw, dict):
        raise ValueError("order response raw payload is not an object")

    data = raw.get("data", raw)

    if not isinstance(data, dict):
        raise ValueError("order snapshot is not an object")

    required = (
        "orderId",
        "clientOrderId",
        "symbol",
        "origQty",
        "executedQty",
        "avgPrice",
        "status",
    )

    missing = [
        key
        for key in required
        if key not in data or data[key] is None
    ]

    if missing:
        raise ValueError(
            "order snapshot missing: " + ", ".join(missing)
        )

    normalized = dict(data)

    normalized["side"] = _canonical_side(
        data.get("side"),
        data.get("positionSide"),
    )

    return normalized


def _normalize_trade(raw):
    """Normalize V2 trade side fields for the existing fill ledger."""
    if not isinstance(raw, dict):
        raise ValueError("trade row must be an object")

    normalized = dict(raw)

    side = normalized.get("side")
    position_side = normalized.get("positionSide")

    if side in LEGACY_TRADE_SIDES:
        return normalized

    normalized["side"] = _canonical_side(
        side,
        position_side,
    )

    return normalized


class OrderRecoveryProbe:
    """Inspect unresolved intents using read-only exchange endpoints."""

    def __init__(self, client, intent_store, trade_limit=1000):
        if client is None or intent_store is None:
            raise ValueError("client and intent_store are required")

        if (
            isinstance(trade_limit, bool)
            or not isinstance(trade_limit, int)
        ):
            raise ValueError("trade_limit must be an integer")

        if not 1 <= trade_limit <= 1000:
            raise ValueError("trade_limit must be from 1 to 1000")

        self.client = client
        self.intent_store = intent_store
        self.trade_limit = trade_limit

    def run(self, now_ms=None):
        """Return a diagnostic report without changing persistent state."""

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

        try:
            unresolved = self.intent_store.recovery_required()

            if not isinstance(unresolved, list):
                raise TypeError(
                    "recovery_required() must return a list"
                )

        except Exception as exc:
            return {
                "ok": False,
                "halt_trading": True,
                "unresolved_count": None,
                "items": [
                    self._failure(
                        None,
                        "intent_journal_unreadable",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                ],
                "journal_mutated": False,
                "trading_state_mutated": False,
                "orders_submitted": 0,
                "orders_amended": 0,
                "orders_canceled": 0,
                "read_only": True,
            }

        items = []

        for record in unresolved:
            items.append(
                self._probe_record(record, timestamp)
            )

        checks_passed = all(
            item.get("can_be_marked_reconciled") is True
            for item in items
        )

        return {
            "ok": checks_passed,
            # Unresolved records always keep trading halted.
            "halt_trading": bool(unresolved),
            "unresolved_count": len(unresolved),
            "items": items,
            "journal_mutated": False,
            "trading_state_mutated": False,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "read_only": True,
        }

    def _get_order_status(self, symbol, client_order_id):
        """Prefer V2 read-only method when available."""
        method = getattr(
            self.client,
            "get_order_status_v2",
            None,
        )

        if callable(method):
            return method(symbol, client_order_id)

        method = getattr(
            self.client,
            "get_order_status",
            None,
        )

        if not callable(method):
            raise RuntimeError(
                "client has no supported read-only order status method"
            )

        return method(symbol, client_order_id)

    def _get_user_trades(
        self,
        symbol,
        start_time,
        end_time,
    ):
        """Prefer V2 read-only method; never silently accept bad results."""
        method = getattr(
            self.client,
            "get_user_trades_v2",
            None,
        )

        if not callable(method):
            method = getattr(
                self.client,
                "get_user_trades",
                None,
            )

        if not callable(method):
            raise RuntimeError(
                "client has no supported read-only user trades method"
            )

        rows = method(
            symbol,
            start_time=start_time,
            end_time=end_time,
            limit=self.trade_limit,
        )

        if not isinstance(rows, list):
            raise ValueError(
                "user trades response is not a list"
            )

        normalized = []

        for index, row in enumerate(rows):
            try:
                normalized.append(_normalize_trade(row))
            except ValueError as exc:
                raise ValueError(
                    f"invalid user trade row {index}: {exc}"
                ) from None

        return normalized

    def _probe_record(self, record, now_ms):
        if not isinstance(record, dict):
            return self._failure(
                None,
                "invalid_intent_record",
            )

        status = record.get("status")
        intent = record.get("intent")
        client_order_id = record.get("client_order_id")

        if (
            not isinstance(intent, dict)
            or not isinstance(client_order_id, str)
            or not client_order_id
        ):
            return self._failure(
                client_order_id,
                "invalid_intent_record",
            )

        if status == "INTENT_CREATED":
            return self._failure(
                client_order_id,
                "intent_created_without_submission_started",
                detail=(
                    "No exchange query made; "
                    "submission boundary is not proven."
                ),
            )

        if status not in PROBE_STATUSES:
            return self._failure(
                client_order_id,
                "unsupported_intent_status",
            )

        symbol = intent.get("symbol")
        created_at = record.get("created_at_ms")

        if (
            not isinstance(symbol, str)
            or not symbol.strip()
        ):
            return self._failure(
                client_order_id,
                "intent_symbol_missing",
            )

        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, int)
            or created_at < 0
        ):
            return self._failure(
                client_order_id,
                "intent_created_at_invalid",
            )

        if now_ms < created_at:
            return self._failure(
                client_order_id,
                "probe_time_before_intent_creation",
            )

        try:
            response = self._get_order_status(
                symbol,
                client_order_id,
            )

            response_status = getattr(
                response,
                "status",
                "UNKNOWN",
            )

            if response_status == "UNKNOWN":
                return self._failure(
                    client_order_id,
                    "exchange_order_status_unknown",
                )

            order = _raw_order(response)

            if response_status != order.get("status"):
                return self._failure(
                    client_order_id,
                    "exchange_status_wrapper_mismatch",
                )

            if str(order["clientOrderId"]) != client_order_id:
                return self._failure(
                    client_order_id,
                    "client_order_id_mismatch",
                )

            if order["symbol"] != symbol:
                return self._failure(
                    client_order_id,
                    "exchange_symbol_mismatch",
                )

            if status == "EXCHANGE_ACKNOWLEDGED":
                expected_id = record.get("exchange_order_id")

                if (
                    expected_id is None
                    or str(order["orderId"]) != str(expected_id)
                ):
                    return self._failure(
                        client_order_id,
                        "exchange_order_id_mismatch",
                    )

            expected_side = SIDE_MAP.get(
                (
                    intent.get("side"),
                    intent.get("intent"),
                )
            )

            if (
                expected_side is None
                or order["side"] != expected_side
            ):
                return self._failure(
                    client_order_id,
                    "exchange_side_mismatch",
                )

            expected_qty = _decimal(
                intent.get("qty"),
                "intent.qty",
            )

            actual_qty = _decimal(
                order.get("origQty"),
                "order.origQty",
            )

            if expected_qty <= 0 or actual_qty <= 0:
                return self._failure(
                    client_order_id,
                    "exchange_quantity_not_positive",
                )

            if expected_qty != actual_qty:
                return self._failure(
                    client_order_id,
                    "exchange_original_quantity_mismatch",
                )

            raw_trades = self._get_user_trades(
                symbol,
                created_at,
                now_ms,
            )

            # A full page may be truncated. Until complete pagination
            # is implemented and verified, this must fail closed.
            if len(raw_trades) >= self.trade_limit:
                return self._failure(
                    client_order_id,
                    "user_trades_result_may_be_truncated",
                    detail=(
                        "Returned row count reached the requested limit; "
                        "complete pagination has not been proven."
                    ),
                )

            order_id = str(order["orderId"])

            order_fills = [
                row
                for row in raw_trades
                if str(row.get("orderId")) == order_id
            ]

            reconciliation = reconcile_order_fills(
                order,
                order_fills,
            )

            if (
                reconciliation.get("ok") is not True
                or reconciliation.get("halt_trading") is not False
            ):
                return self._failure(
                    client_order_id,
                    "fill_reconciliation_failed",
                    detail="; ".join(
                        reconciliation.get("reasons", [])
                    ),
                    exchange_order_id=order_id,
                    exchange_status=order.get("status"),
                    reconciliation=reconciliation,
                )

            if order.get("status") not in TERMINAL_STATUSES:
                return self._failure(
                    client_order_id,
                    "exchange_order_not_terminal",
                    detail=(
                        "Current exchange status: "
                        f"{order.get('status')}"
                    ),
                    exchange_order_id=order_id,
                    exchange_status=order.get("status"),
                    reconciliation=reconciliation,
                )

            return {
                "client_order_id": client_order_id,
                "ok": True,
                "can_be_marked_reconciled": True,
                "reason": (
                    "read_only_exchange_and_fill_checks_passed"
                ),
                "exchange_order_id": order_id,
                "exchange_status": order["status"],
                "reconciliation": reconciliation,
                "manual_journal_transition_required": True,
                "automatic_journal_mutation": False,
            }

        except Exception as exc:
            return self._failure(
                client_order_id,
                "read_only_probe_error",
                detail=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _failure(
        client_order_id,
        reason,
        detail=None,
        **extra,
    ):
        result = {
            "client_order_id": client_order_id,
            "ok": False,
            "can_be_marked_reconciled": False,
            "reason": reason,
            "detail": detail,
        }

        result.update(extra)

        return result
