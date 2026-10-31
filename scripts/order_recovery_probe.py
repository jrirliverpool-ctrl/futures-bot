"""Read-only exchange probe for unresolved Toobit order intents.

This module never places/cancels orders and never mutates the intent journal.
A successful probe is evidence for manual reconciliation only; trading remains
blocked until the journal and trading state are reconciled separately.
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


def _decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} missing or invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} is not numeric")
    if not result.is_finite():
        raise ValueError(f"{field} is not finite")
    return result


def _raw_order(response):
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
        "side",
        "origQty",
        "executedQty",
        "avgPrice",
        "status",
    )

    missing = [
        key for key in required
        if key not in data or data[key] is None
    ]

    if missing:
        raise ValueError(
            "order snapshot missing: " + ", ".join(missing)
        )

    return data


class OrderRecoveryProbe:
    """Inspect unresolved intents using read-only Toobit endpoints."""

    def __init__(self, client, intent_store, trade_limit=1000):
        if client is None or intent_store is None:
            raise ValueError("client and intent_store are required")

        if isinstance(trade_limit, bool) or not isinstance(trade_limit, int):
            raise ValueError("trade_limit must be an integer")

        if not 1 <= trade_limit <= 1000:
            raise ValueError("trade_limit must be from 1 to 1000")

        self.client = client
        self.intent_store = intent_store
        self.trade_limit = trade_limit

    def run(self, now_ms=None):
        """Return a diagnostic report; no journal/state writes are performed."""

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
                "orders_canceled": 0,
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
            # Trading remains blocked while unresolved records exist.
            "halt_trading": bool(unresolved),
            "unresolved_count": len(unresolved),
            "items": items,
            "journal_mutated": False,
            "trading_state_mutated": False,
            "orders_submitted": 0,
            "orders_canceled": 0,
        }

    def _probe_record(self, record, now_ms):
        if not isinstance(record, dict):
            return self._failure(
                None,
                "invalid_intent_record",
            )

        status = record.get("status")
        intent = record.get("intent")
        cid = record.get("client_order_id")

        if (
            not isinstance(intent, dict)
            or not isinstance(cid, str)
            or not cid
        ):
            return self._failure(
                cid,
                "invalid_intent_record",
            )

        if status == "INTENT_CREATED":
            return self._failure(
                cid,
                "intent_created_without_submission_started",
                detail=(
                    "No exchange query made; "
                    "submission boundary is not proven."
                ),
            )

        if status not in PROBE_STATUSES:
            return self._failure(
                cid,
                "unsupported_intent_status",
            )

        symbol = intent.get("symbol")
        created_at = record.get("created_at_ms")

        if not isinstance(symbol, str) or not symbol.strip():
            return self._failure(
                cid,
                "intent_symbol_missing",
            )

        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, int)
            or created_at < 0
        ):
            return self._failure(
                cid,
                "intent_created_at_invalid",
            )

        try:
            response = self.client.get_order_status(
                symbol,
                cid,
            )

            if getattr(response, "status", "UNKNOWN") == "UNKNOWN":
                return self._failure(
                    cid,
                    "exchange_order_status_unknown",
                )

            order = _raw_order(response)

            response_status = getattr(
                response,
                "status",
                None,
            )

            if response_status != order.get("status"):
                return self._failure(
                    cid,
                    "exchange_status_wrapper_mismatch",
                )

            if str(order["clientOrderId"]) != cid:
                return self._failure(
                    cid,
                    "client_order_id_mismatch",
                )

            if order["symbol"] != symbol:
                return self._failure(
                    cid,
                    "exchange_symbol_mismatch",
                )

            if status == "EXCHANGE_ACKNOWLEDGED":
                expected_id = record.get("exchange_order_id")

                if (
                    expected_id is None
                    or str(order["orderId"]) != str(expected_id)
                ):
                    return self._failure(
                        cid,
                        "exchange_order_id_mismatch",
                    )

            expected_side = SIDE_MAP.get(
                (
                    intent.get("side"),
                    intent.get("intent"),
                )
            )

            if expected_side is None or order["side"] != expected_side:
                return self._failure(
                    cid,
                    "exchange_side_mismatch",
                )

            expected_qty = _decimal(
                intent.get("qty"),
                "intent.qty",
            )

            actual_qty = _decimal(
                order["origQty"],
                "order.origQty",
            )

            if expected_qty != actual_qty:
                return self._failure(
                    cid,
                    "exchange_original_quantity_mismatch",
                )

            if now_ms < created_at:
                return self._failure(
                    cid,
                    "probe_time_before_intent_creation",
                )

            raw_trades = self.client.get_user_trades(
                symbol,
                start_time=created_at,
                end_time=now_ms,
                limit=self.trade_limit,
            )

            if not isinstance(raw_trades, list):
                return self._failure(
                    cid,
                    "user_trades_response_not_list",
                )

            if len(raw_trades) >= self.trade_limit:
                return self._failure(
                    cid,
                    "user_trades_result_may_be_truncated",
                    detail=(
                        "Returned row count reached "
                        "the requested limit."
                    ),
                )

            order_id = str(order["orderId"])

            order_fills = [
                row
                for row in raw_trades
                if (
                    isinstance(row, dict)
                    and str(row.get("orderId")) == order_id
                )
            ]

            reconciliation = reconcile_order_fills(
                order,
                order_fills,
            )

            if (
                not reconciliation.get("ok")
                or reconciliation.get("halt_trading")
            ):
                return self._failure(
                    cid,
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
                    cid,
                    "exchange_order_not_terminal",
                    detail=(
                        f"Current exchange status: "
                        f"{order.get('status')}"
                    ),
                    exchange_order_id=order_id,
                    exchange_status=order.get("status"),
                    reconciliation=reconciliation,
                )

            return {
                "client_order_id": cid,
                "ok": True,
                "can_be_marked_reconciled": True,
                "reason": (
                    "read_only_exchange_and_fill_checks_passed"
                ),
                "exchange_order_id": order_id,
                "exchange_status": order["status"],
                "reconciliation": reconciliation,
                "manual_journal_transition_required": True,
            }

        except Exception as exc:
            return self._failure(
                cid,
                "read_only_probe_error",
                detail=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _failure(cid, reason, detail=None, **extra):
        result = {
            "client_order_id": cid,
            "ok": False,
            "can_be_marked_reconciled": False,
            "reason": reason,
            "detail": detail,
        }

        result.update(extra)

        return result