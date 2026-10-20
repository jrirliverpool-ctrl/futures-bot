"""
execution_manager.py — Safe Execution Layer.
"""
import uuid
import logging
import os
from order_executor import evaluate, OrderRequest, Decision, DEFAULT_CONFIG

class ExecutionManager:
    def __init__(self, mode: str, client=None, logger=None):
        if mode not in ["PAPER", "MOCK_LIVE", "TOOBIT_DEMO", "LIVE"]:
            raise ValueError(f"Invalid execution mode: {mode}")
        self.mode = mode
        self.client = client
        self.logger = logger or logging.getLogger("ExecutionManager")

    def execute(self, req: OrderRequest, specs: dict, state: dict, config: dict = None, ref_price: float = 0.0) -> Decision:
        if config is None:
            config = DEFAULT_CONFIG

        # ✅ اگر سفارش نامشخصی وجود دارد، هیچ سفارش جدیدی ارسال نکن
        if state.get("order_status") == "UNKNOWN":
            return Decision(accepted=False, reason="ambiguous_order_halt", gate="safety", order=None)

        # ✅ ۱. بررسی Kill Switch با در نظر گرفتن EXIT
        ks_state = state.get("kill_switch", False) or state.get("circuit_breaker_active", False)
        ks_config = config.get("kill_switch_active", False)
        
        if (ks_state or ks_config):
            if req.intent in ("EXIT", "TP1_HIT"):
                self.logger.warning("Kill Switch active, but EXIT is allowed for emergency closure.")
            else:
                return Decision(accepted=False, reason="kill_switch_active", gate="safety", order=None)

        # ✅ ۲. اعتبارسنجی با order_executor
        decision = evaluate(req, specs, state, config, ref_price)
        if not decision.accepted:
            self.logger.warning(f"Order Rejected: {decision.gate} - {decision.reason}")
            return decision

        # ✅ ۳. مدیریت شناسه سفارش
        client_order_id = state.get("pending_order_id")
        if not client_order_id:
            client_order_id = f"apex_{uuid.uuid4().hex[:12]}"
            state["pending_order_id"] = client_order_id

        if self.mode == "PAPER":
            self.logger.info(f"[PAPER] Simulated: {req.intent} {req.side} {req.qty} {req.symbol}")
            state["pending_order_id"] = None 
            return decision

        elif self.mode == "MOCK_LIVE":
            self.logger.info(f"[MOCK_LIVE] Would send: {req.intent} {req.side} {req.qty} {req.symbol} | ID: {client_order_id}")
            state["pending_order_id"] = None
            return decision

        elif self.mode == "TOOBIT_DEMO":
            if not self.client:
                raise ValueError("Client is required for TOOBIT_DEMO mode.")
            
            self.logger.warning(f"[DEMO] Sending: {req.intent} {req.side} {req.qty} {req.symbol} | ID: {client_order_id}")
            
            # در حالت دمو، اگر سفارش ورود باشد، پارامترهای اضافی ارسال می‌شوند
            response = self.client.place_order(
                symbol=req.symbol, side=req.side, order_type=req.order_type,
                quantity=req.qty, price=req.price, client_order_id=client_order_id
            )
            
            if response.get("status") == "NETWORK_ERROR":
                self.logger.critical(f"Network Error! Order {client_order_id} is UNKNOWN.")
                state["order_status"] = "UNKNOWN"
                return Decision(accepted=False, reason="network_error_unknown", gate="exchange", order=None)
            
            if response.get("status") == "API_ERROR":
                self.logger.error(f"API Rejected: {response}")
                state["pending_order_id"] = None 
                return Decision(accepted=False, reason="exchange_rejected", gate="exchange", order=None)
            
            self.logger.info(f"Demo Order Success: {response}")
            state["pending_order_id"] = None 
            return decision

        elif self.mode == "LIVE":
            if os.environ.get("APEXQUANT_ENV") != "PRODUCTION":
                self.logger.critical("LIVE mode BLOCKED: ENV is not PRODUCTION.")
                return Decision(accepted=False, reason="missing_env_permission", gate="safety", order=None)
            
            if not self.client:
                raise ValueError("Client is required for LIVE mode.")

            exchange_pos = self.client.get_position(req.symbol)
            if "error" in exchange_pos or exchange_pos is None:
                self.logger.error(f"Sync failed: {exchange_pos}")
                return Decision(accepted=False, reason="state_sync_failed", gate="safety", order=None)

            self.logger.warning(f"[LIVE] SENDING: {req.intent} {req.side} {req.qty} {req.symbol} | ID: {client_order_id}")
            response = self.client.place_order(
                symbol=req.symbol, side=req.side, order_type=req.order_type,
                quantity=req.qty, price=req.price, client_order_id=client_order_id
            )
            
            if response.get("status") == "NETWORK_ERROR":
                self.logger.critical(f"Network Error! Order {client_order_id} is UNKNOWN.")
                state["order_status"] = "UNKNOWN"
                return Decision(accepted=False, reason="network_error_unknown", gate="exchange", order=None)
                
            if response.get("status") == "API_ERROR":
                self.logger.error(f"API Rejected: {response}")
                state["pending_order_id"] = None 
                return Decision(accepted=False, reason="exchange_rejected", gate="exchange", order=None)
            
            self.logger.info(f"Order Success: {response}")
            state["pending_order_id"] = None 
            return decision