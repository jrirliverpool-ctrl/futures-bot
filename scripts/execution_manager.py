"""
execution_manager.py — Safe Execution Layer.
Modes: PAPER, MOCK_LIVE, LIVE.
"""
import uuid
import logging
import os
from order_executor import evaluate, OrderRequest, Decision, DEFAULT_CONFIG

class ExecutionManager:
    def __init__(self, mode: str, client=None, logger=None):
        if mode not in ["PAPER", "MOCK_LIVE", "LIVE"]:
            raise ValueError(f"Invalid execution mode: {mode}")
        self.mode = mode
        self.client = client
        self.logger = logger or logging.getLogger("ExecutionManager")

    def execute(self, req: OrderRequest, specs: dict, state: dict, config: dict = None, ref_price: float = 0.0) -> Decision:
        if config is None:
            config = DEFAULT_CONFIG

        # 1. گیت‌های ایمنی اولیه (Fail-Fast)
        if state.get("kill_switch", False) or state.get("circuit_breaker_active", False):
            return Decision(accepted=False, reason="kill_switch_active", gate="safety", order=None)

        # 2. دروازه‌بان ریسک
        decision = evaluate(req, specs, state, config, ref_price)
        if not decision.accepted:
            self.logger.warning(f"Order Rejected: {decision.gate} - {decision.reason}")
            return decision

        # 3. مدیریت شناسه پایدار (Persistent ID)
        # اگر در استیت آیدی قبلی وجود دارد (مثلاً بعد از ری‌استارت)، از همان استفاده کن
        client_order_id = state.get("pending_order_id")
        if not client_order_id:
            client_order_id = f"apex_{uuid.uuid4().hex[:12]}"
            state["pending_order_id"] = client_order_id

        # 4. حالت PAPER
        if self.mode == "PAPER":
            self.logger.info(f"[PAPER] Simulated: {req.side} {req.qty} {req.symbol}")
            state["pending_order_id"] = None # آزادسازی پس از شبیه‌سازی
            return decision

        # 5. حالت MOCK_LIVE
        elif self.mode == "MOCK_LIVE":
            self.logger.info(f"[MOCK_LIVE] Would send: {req.side} {req.qty} {req.symbol} | ID: {client_order_id}")
            state["pending_order_id"] = None
            return decision

        # 6. حالت LIVE
        elif self.mode == "LIVE":
            # گیت مجوز محیطی
            if os.environ.get("APEXQUANT_ENV") != "PRODUCTION":
                self.logger.critical("LIVE mode BLOCKED: ENV is not PRODUCTION.")
                return Decision(accepted=False, reason="missing_env_permission", gate="safety", order=None)
            
            if not self.client:
                raise ValueError("Client is required for LIVE mode.")

            # همگام‌سازی با صرافی
            exchange_pos = self.client.get_position(req.symbol)
            if "error" in exchange_pos:
                self.logger.error(f"Sync failed: {exchange_pos['error']}")
                return Decision(accepted=False, reason="state_sync_failed", gate="safety", order=None)

            # ارسال سفارش
            self.logger.warning(f"[LIVE] SENDING: {req.side} {req.qty} {req.symbol} | ID: {client_order_id}")
            response = self.client.place_order(
                symbol=req.symbol, side=req.side, order_type=req.order_type,
                quantity=req.qty, price=req.price, client_order_id=client_order_id
            )
            
            # اگر خطای شبکه در ارسال رخ داد
            if response.get("status") == "NETWORK_ERROR":
                self.logger.critical(f"Network Error! Order {client_order_id} is UNKNOWN.")
                return Decision(accepted=False, reason="network_error_unknown", gate="exchange", order=None)
                
            # اگر خطای API رخ داد
            if response.get("status") == "API_ERROR":
                self.logger.error(f"API Rejected: {response}")
                state["pending_order_id"] = None # اجازه تلاش مجدد در سیگنال بعدی
                return Decision(accepted=False, reason="exchange_rejected", gate="exchange", order=None)
            
            # موفقیت
            self.logger.info(f"Order Success: {response}")
            state["pending_order_id"] = None # آزادسازی پس از ثبت موفق
            return decision