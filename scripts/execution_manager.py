"""
execution_manager.py — Safe Execution Layer.
v2.2 — Final fixes:
  - NEW: save exchange_order_id + halt (caller must query next cycle)
  - FILLED: validate exchange_order_id too
  - PARTIALLY_FILLED: validate exchange_order_id
  - Uses StandardOrderResponse contract
"""

import uuid
import logging
import math
from order_executor import evaluate, OrderRequest, Decision, DEFAULT_CONFIG
from toobit_client import StandardOrderResponse


class ExecutionManager:
    def __init__(self, mode: str, client=None, logger=None):
        if mode not in ["PAPER", "MOCK_LIVE", "TOOBIT_DEMO", "LIVE"]:
            raise ValueError(f"Invalid mode: {mode}")
        self.mode = mode
        self.client = client
        self.logger = logger or logging.getLogger("ExecutionManager")

    def execute(self, req: OrderRequest, specs: dict, state: dict,
                config: dict = None, ref_price: float = 0.0) -> Decision:
        if config is None:
            config = DEFAULT_CONFIG

        # 1. Halt & UNKNOWN
        if state.get("order_status") == "UNKNOWN":
            self.logger.critical("HALT: Ambiguous order exists. Manual review.")
            return Decision(accepted=False, reason="unknown_order_halt", gate="safety", order=None)

        if state.get("halt_trading"):
            if req.intent not in ("EXIT", "TP1_HIT"):
                return Decision(accepted=False, reason="halt_trading_blocked", gate="safety", order=None)
            else:
                self.logger.warning(f"HALT: Allowing {req.intent} to close position.")

        # 2. Validate
        decision = evaluate(req, specs, state, config, ref_price)
        if not decision.accepted:
            return decision

        # 3. Order ID
        client_order_id = state.get("pending_order_id")
        if not client_order_id:
            client_order_id = f"apex_{uuid.uuid4().hex[:12]}"
            state["pending_order_id"] = client_order_id

        # 4. Execute
        if self.mode in ("PAPER", "MOCK_LIVE"):
            self.logger.info(f"[{self.mode}] Would send: {req.intent} {req.side} {req.qty} {req.symbol}")
            state["pending_order_id"] = None
            return decision

        if self.mode in ("TOOBIT_DEMO", "LIVE"):
            if not self.client:
                raise ValueError("Client required")

            try:
                resp: StandardOrderResponse = self.client.place_order(
                    symbol=req.symbol, side=req.side,
                    order_type=req.order_type, quantity=req.qty,
                    price=req.price, client_order_id=client_order_id,
                    intent=req.intent,
                )
            except Exception as e:
                self.logger.critical(f"Client exception! {client_order_id} UNKNOWN: {e}")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="client_exception_unknown", gate="exchange", order=None)

            return self._process_response(resp, req, state, client_order_id, decision)

        raise ValueError(f"Unhandled mode: {self.mode}")

    def _process_response(self, resp, req, state, client_order_id, decision) -> Decision:
        # ---- FILLED ----
        if resp.status == "FILLED":
            valid = (
                math.isfinite(resp.filled_qty)
                and resp.filled_qty > 0
                and abs(resp.filled_qty - req.qty) <= 1e-8
                and resp.symbol == req.symbol
                and resp.client_order_id == client_order_id
                and resp.exchange_order_id is not None
            )
            if not valid:
                self.logger.critical(
                    f"FILLED validation failed: filled={resp.filled_qty}, req={req.qty}, "
                    f"symbol={resp.symbol}, cli_id={resp.client_order_id}, exch_id={resp.exchange_order_id}"
                )
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="filled_validation_failed", gate="exchange", order=None)

            self.logger.info(f"Order FILLED: {resp.exchange_order_id}")
            state["order_status"] = "FILLED"
            state["pending_order_id"] = None
            state["last_exchange_order_id"] = resp.exchange_order_id
            return decision

        # ---- PARTIALLY_FILLED ----
        if resp.status == "PARTIALLY_FILLED":
            self.logger.critical(f"{client_order_id} PARTIALLY FILLED. Halting.")
            valid_partial = (
                math.isfinite(resp.filled_qty)
                and resp.filled_qty > 0
                and resp.filled_qty < req.qty
                and resp.exchange_order_id is not None
            )
            if valid_partial:
                state["partial_fill_qty"] = resp.filled_qty
                state["partial_fill_exchange_id"] = resp.exchange_order_id
                state["partial_fill_data_invalid"] = False
            else:
                state["partial_fill_qty"] = None
                state["partial_fill_exchange_id"] = None
                state["partial_fill_data_invalid"] = True
            state["order_status"] = "UNKNOWN"
            state["halt_trading"] = True
            return Decision(accepted=False, reason="partial_fill_halt", gate="exchange", order=None)

        # ---- NEW: Save identity, halt for next-cycle query ----
        if resp.status == "NEW":
            self.logger.warning(
                f"{client_order_id} accepted but status=NEW. Saving exchange_order_id for later recovery."
            )
            # v2.2: Save identity so next cycle can query
            if resp.exchange_order_id is not None:
                state["pending_exchange_order_id"] = resp.exchange_order_id
            # Keep pending_order_id (don't clear)
            # Halt: no new orders until this one is resolved
            state["order_status"] = "PENDING_FILL"
            state["halt_trading"] = True
            return Decision(
                accepted=False,
                reason="new_status_pending_recovery",
                gate="exchange",
                order=None
            )

        # ---- CANCELED ----
        if resp.status == "CANCELED":
            if resp.filled_qty > 0:
                self.logger.critical(f"CANCELED with partial fill {resp.filled_qty}. Halting.")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="canceled_with_partial", gate="exchange", order=None)
            self.logger.info(f"Order CANCELED with zero fill.")
            state["order_status"] = "CANCELED"
            state["pending_order_id"] = None
            return Decision(accepted=False, reason="canceled_no_fill", gate="exchange", order=None)

        # ---- REJECTED ----
        if resp.status == "REJECTED":
            self.logger.info(f"Order REJECTED.")
            state["order_status"] = "REJECTED"
            state["pending_order_id"] = None
            return Decision(accepted=False, reason="exchange_rejected", gate="exchange", order=None)

        # ---- UNKNOWN ----
        self.logger.critical(f"Order {client_order_id} status UNKNOWN. Halting.")
        state["order_status"] = "UNKNOWN"
        state["halt_trading"] = True
        return Decision(accepted=False, reason="unknown_status", gate="exchange", order=None)