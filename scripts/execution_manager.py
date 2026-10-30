import uuid
import logging
import math

from order_executor import (
    evaluate,
    OrderRequest,
    Decision,
    DEFAULT_CONFIG,
)
from toobit_client import StandardOrderResponse


class ExecutionManager:
    """
    Central order execution manager.

    Safety policy:
    - PAPER and MOCK_LIVE are supported.
    - TOOBIT_DEMO and LIVE are intentionally blocked.
    - Real execution must not be enabled until actual exchange fills
      are reconciled into the strategy state and accounting ledger.
    """

    ALLOWED_MODES = (
        "PAPER",
        "MOCK_LIVE",
        "TOOBIT_DEMO",
        "LIVE",
    )

    BLOCKED_REAL_MODES = (
        "TOOBIT_DEMO",
        "LIVE",
    )

    def __init__(self, mode: str, client=None, logger=None):
        if mode not in self.ALLOWED_MODES:
            raise ValueError(f"Invalid mode: {mode}")

        self.mode = mode
        self.client = client
        self.logger = logger or logging.getLogger("ExecutionManager")

        if self.mode in self.BLOCKED_REAL_MODES:
            self.logger.critical(
                "REAL EXECUTION MODE IS DISABLED BY SAFETY POLICY."
            )

    def _blocked_real_execution(self) -> Decision:
        """
        Fail-closed response.

        This check must happen before:
        - querying exchange order status
        - generating an exchange order ID
        - persisting a new exchange order intent
        - sending any network request
        """

        self.logger.critical(
            "Order blocked: real execution is not reconciled "
            "with internal position and accounting state."
        )

        return Decision(
            accepted=False,
            reason="real_execution_disabled_pending_reconciliation",
            gate="safety",
            order=None,
        )

    def execute(
        self,
        req: OrderRequest,
        specs: dict,
        state: dict,
        config: dict = None,
        ref_price: float = 0.0,
        save_state_fn=None,
    ) -> Decision:

        if config is None:
            config = DEFAULT_CONFIG

        # ---------------------------------------------------------
        # P0 SAFETY LOCK
        # Must execute before recovery or any exchange interaction.
        # ---------------------------------------------------------

        if self.mode in self.BLOCKED_REAL_MODES:
            return self._blocked_real_execution()

        # ---------------------------------------------------------
        # 1. Recovery protection
        # ---------------------------------------------------------

        if state.get("order_status") == "PENDING_FILL":

            self.logger.critical(
                "Unresolved PENDING_FILL state detected."
            )

            return Decision(
                accepted=False,
                reason="recovery_requires_exchange_reconciliation",
                gate="safety",
                order=None,
            )

        if state.get("order_status") == "UNKNOWN":
            return Decision(
                accepted=False,
                reason="unknown_order_state",
                gate="safety",
                order=None,
            )

        if state.get("halt_trading"):
            return Decision(
                accepted=False,
                reason="halt_trading_active",
                gate="safety",
                order=None,
            )

        # ---------------------------------------------------------
        # 2. Validate request
        # ---------------------------------------------------------

        decision = evaluate(
            req,
            specs,
            state,
            config,
            ref_price,
        )

        if not decision.accepted:
            return decision

        # ---------------------------------------------------------
        # 3. Prevent stale pending-order state
        # ---------------------------------------------------------

        if state.get("pending_order_id") is not None:

            self.logger.critical(
                "Existing pending_order_id detected. "
                "New order blocked."
            )

            return Decision(
                accepted=False,
                reason="stale_pending_order_id",
                gate="safety",
                order=None,
            )

        # ---------------------------------------------------------
        # 4. PAPER / MOCK_LIVE only
        # No exchange request is sent in these modes.
        # ---------------------------------------------------------

        if self.mode in ("PAPER", "MOCK_LIVE"):

            self.logger.info(
                "Simulation order accepted: mode=%s intent=%s symbol=%s",
                self.mode,
                req.intent,
                req.symbol,
            )

            return decision

        # ---------------------------------------------------------
        # 5. Defensive fallback
        # ---------------------------------------------------------

        self.logger.critical(
            "Unhandled execution mode: %s",
            self.mode,
        )

        return Decision(
            accepted=False,
            reason="unsupported_execution_mode",
            gate="safety",
            order=None,
        )

    def _process_response(
        self,
        resp,
        req,
        state,
        client_order_id,
        decision,
        save_state_fn,
    ) -> Decision:

        """
        Exchange response processing is deliberately disabled.

        A future implementation must reconcile:
        - exchange order ID
        - client order ID
        - symbol
        - requested quantity
        - filled quantity
        - average fill price
        - partial fills
        - commissions
        - actual position state
        - internal equity and realized PnL

        It must not simply return accepted=True after FILLED.
        """

        self.logger.critical(
            "Exchange response processing is disabled until "
            "actual-fill reconciliation is implemented."
        )

        state["order_status"] = "UNKNOWN"
        state["halt_trading"] = True

        if save_state_fn is not None:
            save_state_fn(state)

        return Decision(
            accepted=False,
            reason="exchange_fill_reconciliation_not_implemented",
            gate="safety",
            order=None,
        )