"""
execution_manager.py — Safe Execution Layer.
v1.2 — Fixes: UNKNOWN halts trading. Stable order ID.
"""
import uuid
import logging
import os
from order_executor import evaluate, OrderRequest, Decision, DEFAULT_CONFIG

class ExecutionManager:
    def __init__(self, mode: str, client=None, logger=None):
        if mode not in ["PAPER", "MOCK_LIVE", "TOOBIT_DEMO", "LIVE"]:
            raise ValueError(f"Invalid mode: {mode}")
        self.mode = mode
        self.client = client
        self.logger = logger or logging.getLogger("ExecutionManager")

    def execute(self, req: OrderRequest, specs: dict, state: dict, config: dict = None, ref_price: float = 0.0) -> Decision:
        if config is None: config = DEFAULT_CONFIG

        # 1. Halt on UNKNOWN or previous HALT
        if state.get("order_status") == "UNKNOWN" or state.get("halt_trading"):
            self.logger.critical("HALT: Ambiguous order exists or system halted.")
            return Decision(accepted=False, reason="ambiguous_order_halt", gate="safety", order=None)

        # 2. Validate
        decision = evaluate(req, specs, state, config, ref_price)
        if not decision.accepted:
            self.logger.warning(f"Order Rejected: {decision.gate} - {decision.reason}")
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

        elif self.mode in ("TOOBIT_DEMO", "LIVE"):
            if not self.client: raise ValueError("Client required")
            
            exchange_side = "BUY" if (req.side == "LONG" and req.intent == "ENTRY") or (req.side == "SHORT" and req.intent in ("EXIT", "TP1_HIT")) else "SELL"

            self.logger.warning(f"[{self.mode}] Sending: {req.intent} {exchange_side} {req.qty} {req.symbol}")
            
            try:
                response = self.client.place_order(
                    symbol=req.symbol, side=exchange_side, order_type=req.order_type,
                    quantity=req.qty, price=req.price, client_order_id=client_order_id
                )
            except Exception as e:
                self.logger.critical(f"Network Exception! Order {client_order_id} is UNKNOWN. Error: {e}")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="network_exception_unknown", gate="exchange", order=None)

            if not isinstance(response, dict) or response.get("status") in ("NETWORK_ERROR", "UNKNOWN"):
                self.logger.critical(f"Invalid/Unknown response: {response}. Order UNKNOWN.")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="invalid_response_unknown", gate="exchange", order=None)
                
            if response.get("status") == "API_ERROR":
                self.logger.error(f"API Rejected: {response}")
                state["pending_order_id"] = None
                return Decision(accepted=False, reason="exchange_rejected", gate="exchange", order=None)

            if response.get("status") not in ("SUCCESS", "FILLED"):
                self.logger.critical(f"Unknown status '{response.get('status')}'. Order UNKNOWN.")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="unknown_status", gate="exchange", order=None)

            self.logger.info(f"Order Success: {response}")
            state["pending_order_id"] = None
            return decision