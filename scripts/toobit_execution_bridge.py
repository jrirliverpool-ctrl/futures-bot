"""ApexQuant controlled execution bridge.

This module connects order validation to the durable intent journal.

IMPORTANT:
- This module does NOT submit orders.
- It does NOT contact Toobit.
- It does NOT change account settings.
- It records an order intent only after validation succeeds.
- Any unresolved journal entry blocks preparation of another order.

A separate, verified fill-reconciliation and exchange-protection layer
is required before any live submission can be enabled.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from order_executor import (
    DEFAULT_CONFIG,
    OrderRequest,
    evaluate,
)
from order_intent_store import (
    IntentJournalError,
    OrderIntentStore,
)


class BridgeSafetyError(RuntimeError):
    """Raised when an unsafe execution transition is attempted."""


@dataclass(frozen=True)
class BridgeResult:
    accepted: bool
    stage: str
    reason: str | None
    gate: str | None
    client_order_id: str | None
    journal_status: str | None


class ToobitExecutionBridge:
    """Validated order preparation with durable write-ahead journaling.

    This bridge intentionally stops before exchange submission.
    """

    def __init__(self, intent_store: OrderIntentStore):
        if intent_store is None:
            raise ValueError("intent_store is required")

        required = (
            "recovery_required",
            "create_intent",
            "get",
        )

        for method in required:
            if not callable(getattr(intent_store, method, None)):
                raise ValueError(
                    f"intent_store is missing required method: {method}"
                )

        self.intent_store = intent_store

    @staticmethod
    def _reject(reason: str, gate: str = "bridge") -> BridgeResult:
        return BridgeResult(
            accepted=False,
            stage="REJECTED",
            reason=reason,
            gate=gate,
            client_order_id=None,
            journal_status=None,
        )

    @staticmethod
    def _positive_decimal(value, field: str) -> Decimal:
        if value is None or isinstance(value, bool):
            raise ValueError(f"{field} must be a positive decimal")

        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"{field} must be numeric") from None

        if not result.is_finite() or result <= 0:
            raise ValueError(f"{field} must be finite and > 0")

        return result

    @staticmethod
    def _required_text(value, field: str, max_length: int = 128) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")

        normalized = value.strip()

        if not normalized or len(normalized) > max_length:
            raise ValueError(
                f"{field} must be non-empty and <= {max_length} characters"
            )

        return normalized

    @classmethod
    def _make_client_order_id(
        cls,
        req: OrderRequest,
        signal_id: str,
        strategy_hash: str,
    ) -> str:
        """Create a deterministic exchange-compatible client order ID."""

        payload = {
            "symbol": req.symbol,
            "side": req.side,
            "intent": req.intent,
            "order_type": req.order_type,
            "qty": str(cls._positive_decimal(req.qty, "qty")),
            "price": (
                str(cls._positive_decimal(req.price, "price"))
                if req.price is not None
                else None
            ),
            "leverage": req.leverage,
            "signal_id": signal_id,
            "strategy_hash": strategy_hash,
        }

        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

        digest = hashlib.sha256(encoded).hexdigest()

        # Short, deterministic, and below the journal's 64-char limit.
        return "aq" + digest[:32]

    def prepare_order(
        self,
        *,
        req: OrderRequest,
        specs: dict,
        state: dict,
        signal_id: str,
        strategy_hash: str,
        config: dict | None = None,
        ref_price: float = 0.0,
        now_ms: int | None = None,
    ) -> BridgeResult:
        """Validate and durably record an order intent without submitting it.

        The caller must supply current, verified exchange specifications
        and a state snapshot that has already passed reconciliation.

        This method does not claim that the supplied state is exchange-truth.
        """

        if not isinstance(req, OrderRequest):
            return self._reject("invalid_order_request", "shape")

        if not isinstance(specs, dict):
            return self._reject("invalid_exchange_specs", "specs")

        if not isinstance(state, dict):
            return self._reject("invalid_execution_state", "state")

        try:
            signal_id = self._required_text(
                signal_id,
                "signal_id",
                128,
            )
            strategy_hash = self._required_text(
                strategy_hash,
                "strategy_hash",
                128,
            )
        except ValueError as exc:
            return self._reject(str(exc), "identity")

        if now_ms is not None and (
            isinstance(now_ms, bool)
            or not isinstance(now_ms, int)
            or now_ms < 0
        ):
            return self._reject("invalid_now_ms", "clock")

        try:
            qty = self._positive_decimal(req.qty, "qty")
        except ValueError as exc:
            return self._reject(str(exc), "quantity")

        if req.order_type == "MARKET":
            if req.price is not None:
                return self._reject(
                    "market_order_must_not_have_price",
                    "price",
                )
            price = None

        elif req.order_type == "LIMIT":
            try:
                price = self._positive_decimal(req.price, "price")
            except ValueError as exc:
                return self._reject(str(exc), "price")

        else:
            return self._reject("unsupported_order_type", "shape")

        if (
            isinstance(req.leverage, bool)
            or not isinstance(req.leverage, int)
            or req.leverage < 1
        ):
            return self._reject("invalid_leverage", "leverage")

        if (
            isinstance(ref_price, bool)
            or not isinstance(ref_price, (int, float))
            or not math.isfinite(ref_price)
            or ref_price < 0
        ):
            return self._reject("invalid_reference_price", "price")

        # ---------------------------------------------------------
        # 1. Recovery guard
        # ---------------------------------------------------------

        try:
            unresolved = self.intent_store.recovery_required()
        except Exception:
            return self._reject(
                "intent_journal_unreadable",
                "recovery",
            )

        if not isinstance(unresolved, list):
            return self._reject(
                "intent_recovery_result_invalid",
                "recovery",
            )

        if unresolved:
            return self._reject(
                "unresolved_order_intent_recovery_required",
                "recovery",
            )

        # ---------------------------------------------------------
        # 2. Strategy and portfolio validation
        # ---------------------------------------------------------

        decision = evaluate(
            req,
            specs,
            state,
            config if config is not None else DEFAULT_CONFIG,
            ref_price=ref_price,
        )

        if not decision.accepted:
            return self._reject(
                decision.reason or "order_validation_failed",
                decision.gate or "validation",
            )

        # ---------------------------------------------------------
        # 3. Deterministic order identity
        # ---------------------------------------------------------

        try:
            client_order_id = self._make_client_order_id(
                req,
                signal_id,
                strategy_hash,
            )
        except (TypeError, ValueError):
            return self._reject(
                "could_not_create_order_identity",
                "identity",
            )

        # ---------------------------------------------------------
        # 4. Durable write-ahead intent
        # ---------------------------------------------------------

        intent = {
            "client_order_id": client_order_id,
            "symbol": req.symbol,
            "side": req.side,
            "intent": req.intent,
            "order_type": req.order_type,
            "qty": str(qty),
            "price": str(price) if price is not None else None,
            "leverage": req.leverage,
            "signal_id": signal_id,
            "strategy_hash": strategy_hash,
        }

        try:
            record = self.intent_store.create_intent(
                intent,
                now_ms=now_ms,
            )
        except (IntentJournalError, OSError, ValueError):
            return self._reject(
                "intent_journal_write_failed",
                "journal",
            )
        except Exception:
            return self._reject(
                "intent_journal_unexpected_error",
                "journal",
            )

        if not isinstance(record, dict):
            return self._reject(
                "intent_journal_returned_invalid_record",
                "journal",
            )

        if record.get("client_order_id") != client_order_id:
            return self._reject(
                "intent_journal_identity_mismatch",
                "journal",
            )

        if record.get("status") != "INTENT_CREATED":
            return self._reject(
                "intent_not_in_initial_state",
                "journal",
            )

        return BridgeResult(
            accepted=True,
            stage="INTENT_RECORDED_NOT_SUBMITTED",
            reason=(
                "validated_and_journaled; exchange_submission_is_disabled"
            ),
            gate=None,
            client_order_id=client_order_id,
            journal_status=record["status"],
        )

    def submit_order(self, *args, **kwargs) -> BridgeResult:
        """Explicitly disabled: never submit an exchange order here."""

        raise BridgeSafetyError(
            "exchange_submission_disabled: "
            "verified fill reconciliation, persistent live state, "
            "and exchange-side protection are not yet integrated"
        )