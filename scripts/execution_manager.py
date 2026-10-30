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

    def execute(self, req: OrderRequest, specs: dict, state: dict, config: dict = None, ref_price: float = 0.0, save_state_fn=None) -> Decision:
        if config is None: config = DEFAULT_CONFIG

        # 1. Halt & Recovery Logic
        if state.get("order_status") == "PENDING_FILL":
            self.logger.critical("RECOVERY: Querying PENDING_FILL order...")
            if self.mode not in ("TOOBIT_DEMO", "LIVE"):
                return Decision(accepted=False, reason="recovery_requires_live", gate="safety", order=None)
            
            resp = self.client.get_order_status(req.symbol, state.get("pending_order_id"))
            if resp.status == "FILLED":
                state["order_status"] = "FILLED"
                state["halt_trading"] = False
                state["pending_order_id"] = None
                self.logger.info("Recovery: Order FILLED.")
                return Decision(accepted=True, reason="recovered_fill", gate=None, order=None)
            elif resp.status == "CANCELED" and resp.filled_qty == 0:
                state["order_status"] = "CANCELED"
                state["halt_trading"] = False
                state["pending_order_id"] = None
                self.logger.info("Recovery: Order CANCELED clean.")
                return Decision(accepted=False, reason="recovered_cancel", gate=None, order=None)
            else:
                self.logger.critical(f"Recovery failed. Status={resp.status}, Filled={resp.filled_qty}")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                return Decision(accepted=False, reason="recovery_unresolved", gate="safety", order=None)

        if state.get("order_status") == "UNKNOWN" or state.get("halt_trading"):
            if req.intent not in ("EXIT", "TP1_HIT"):
                return Decision(accepted=False, reason="halt_trading_blocked", gate="safety", order=None)
            else:
                self.logger.warning(f"HALT: Allowing {req.intent} to close position.")

        # 2. Validate
        decision = evaluate(req, specs, state, config, ref_price)
        if not decision.accepted:
            return decision

        # 3. Order ID (CRITICAL: Must be new if previous was resolved)
        if state.get("pending_order_id") is not None:
            self.logger.critical("HALT: Existing pending_order_id found but status is not PENDING_FILL. Blocking new order.")
            return Decision(accepted=False, reason="stale_pending_order_id", gate="safety", order=None)
            
        client_order_id = f"apex_{uuid.uuid4().hex[:12]}"
        state["pending_order_id"] = client_order_id

        # 4. Save State BEFORE Network Request
        if save_state_fn:
            save_state_fn(state)
            self.logger.info(f"State persisted with pending_order_id={client_order_id}")

        # 5. Execute
        if self.mode in ("PAPER", "MOCK_LIVE"):
            state["pending_order_id"] = None
            return decision

        if self.mode in ("TOOBIT_DEMO", "LIVE"):
            if not self.client: raise ValueError("Client required")

            try:
                resp: StandardOrderResponse = self.client.place_order(
                    symbol=req.symbol, side=req.side, order_type=req.order_type,
                    quantity=req.qty, price=req.price, client_order_id=client_order_id,
                    intent=req.intent,
                )
            except Exception as e:
                self.logger.critical(f"Client exception! {client_order_id} UNKNOWN: {e}")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
                if save_state_fn: save_state_fn(state)
                return Decision(accepted=False, reason="client_exception_unknown", gate="exchange", order=None)

            return self._process_response(resp, req, state, client_order_id, decision, save_state_fn)

        raise ValueError(f"Unhandled mode: {self.mode}")

    def _process_response(self, resp, req, state, client_order_id, decision, save_state_fn) -> Decision:
        if resp.status == "FILLED":
            valid = (
                math.isfinite(resp.filled_qty) and resp.filled_qty > 0
                and abs(resp.filled_qty - req.qty) <= 1e-8
                and math.isfinite(resp.requested_qty) and abs(resp.requested_qty - req.qty) <= 1e-8
                and resp.avg_price is not None and math.isfinite(resp.avg_price) and resp.avg_price > 0
                and resp.symbol == req.symbol
                and resp.client_order_id == client_order_id
                and resp.exchange_order_id is not None
            )
            if not valid:
                self.logger.critical(f"FILLED validation failed: {resp}")
                state["order_status"] = "UNKNOWN"
                state["halt_trading"] = True
            else:
                self.logger.info(f"Order FILLED: {resp.exchange_order_id}")
                state["order_status"] = "FILLED"
                state["pending_order_id"] = None
                state["last_exchange_order_id"] = resp.exchange_order_id
            if save_state_fn: save_state_fn(state)
            return decision if valid else Decision(accepted=False, reason="filled_validation_failed", gate="exchange", order=None)

        if resp.status == "PARTIALLY_FILLED":
            valid_partial = (
                math.isfinite(resp.filled_qty) and resp.filled_qty > 0
                and resp.symbol == req.symbol
                and resp.client_order_id == client_order_id
            )
            if valid_partial:
                state["partial_fill_qty"] = resp.filled_qty
                state["partial_fill_data_invalid"] = False
            else:
                state["partial_fill_qty"] = None
                state["partial_fill_data_invalid"] = True
            state["order_status"] = "UNKNOWN"
            state["halt_trading"] = True
            if save_state_fn: save_state_fn(state)
            return Decision(accepted=False, reason="partial_fill_halt", gate="exchange", order=None)

        if resp.status == "NEW":
            state["order_status"] = "PENDING_FILL"
            state["halt_trading"] = True
            if save_state_fn: save_state_fn(state)
            return Decision(accepted=False, reason="new_status_pending", gate="exchange", order=None)

        if resp.status == "CANCELED":
            if resp.client_order_id == client_order_id and resp.symbol == req.symbol and resp.filled_qty == 0:
                state["order_status"] = "CANCELED"
                state["pending_order_id"] = None
                if save_state_fn: save_state_fn(state)
                return Decision(accepted=False, reason="canceled_no_fill", gate="exchange", order=None)
            state["order_status"] = "UNKNOWN"
            state["halt_trading"] = True
            if save_state_fn: save_state_fn(state)
            return Decision(accepted=False, reason="canceled_mismatch", gate="exchange", order=None)

        # UNKNOWN / REJECTED
        state["order_status"] = "UNKNOWN"
        state["halt_trading"] = True
        if save_state_fn: save_state_fn(state)
        return Decision(accepted=False, reason="unknown_status", gate="exchange", order=None)