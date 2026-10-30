"""
toobit_client.py — Toobit API V1 client.
v2.2 — Final fixes:
  - _is_success handles both direct order dict AND wrapped {code:200}
  - NEW: caller must save exchange_order_id before halting
  - Market order: no `price` field sent
  - executedQty > origQty → UNKNOWN
  - origQty=0 → use requested_qty fallback (safe for filled orders)
  - Auth preserved exactly: HMAC-SHA256 + urllib.urlencode(sorted) + X-BB-APIKEY
"""

import os
import time
import hmac
import hashlib
import logging
import math
import urllib.parse
import requests
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.toobit.com"
VALID_STATUSES = ("NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED")


@dataclass
class StandardOrderResponse:
    exchange_order_id: Optional[str]
    client_order_id: Optional[str]
    symbol: str
    requested_qty: float
    filled_qty: float
    avg_price: Optional[float]
    status: str
    raw_response: dict


class ToobitClient:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self.api_key = api_key or os.environ.get("TOOBIT_API_KEY")
        self.api_secret = api_secret or os.environ.get("TOOBIT_API_SECRET")
        if not self.api_key or not self.api_secret:
            raise ValueError("API credentials required")

    def _generate_signature(self, params: dict) -> str:
        query_string = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _request(self, method: str, path: str, params: dict) -> dict:
        url = f"{BASE_URL}{path}"
        headers = {"X-BB-APIKEY": self.api_key}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = self._generate_signature(params)

        try:
            if method == "GET":
                r = requests.get(url, params=params, headers=headers, timeout=15)
            elif method == "POST":
                r = requests.post(url, data=params, headers=headers, timeout=15)
            else:
                raise ValueError(f"Unsupported method: {method}")
        except requests.exceptions.Timeout:
            return {"_transport": "NETWORK_ERROR", "error": "timeout"}
        except requests.exceptions.RequestException as e:
            return {"_transport": "NETWORK_ERROR", "error": str(e)}

        try:
            return r.json()
        except ValueError:
            return {"_transport": "UNKNOWN", "error": f"non-JSON (HTTP {r.status_code})"}

    def _map_side(self, side: str, intent: str) -> str:
        if side == "LONG" and intent == "ENTRY": return "BUY_OPEN"
        if side == "LONG" and intent in ("EXIT", "TP1_HIT"): return "SELL_CLOSE"
        if side == "SHORT" and intent == "ENTRY": return "SELL_OPEN"
        if side == "SHORT" and intent in ("EXIT", "TP1_HIT"): return "BUY_CLOSE"
        raise ValueError(f"Invalid side/intent: {side}/{intent}")

    def _is_success(self, data: dict) -> bool:
        """
        v2.2 Fix: Handles both:
          (a) Direct order object: {orderId, status, executedQty, ...} → success
          (b) Wrapped response: {code: 200, data: ...} → success
        Returns False for transport errors, non-dict, or error codes.
        """
        if not isinstance(data, dict):
            return False
        if data.get("_transport"):
            return False

        # Case (a): direct order response
        if "orderId" in data or "status" in data:
            code = data.get("code")
            if code is not None and code not in (200, 0):
                return False
            return True

        # Case (b): wrapped response
        code = data.get("code")
        return code in (200, 0)

    def _parse_order(self, data: dict, requested_qty: float) -> StandardOrderResponse:
        def _safe_float(v):
            try:
                f = float(v)
                if math.isfinite(f) and f >= 0:
                    return f
            except (ValueError, TypeError):
                pass
            return None

        executed = _safe_float(data.get("executedQty"))
        orig = _safe_float(data.get("origQty"))

        if executed is None:
            return self._unknown_from(data, requested_qty, "executedQty invalid")

        # v2.2: executedQty must not exceed origQty
        if orig is not None and orig > 0 and executed > orig + 1e-8:
            return self._unknown_from(data, requested_qty, "executedQty > origQty")

        # Fallback for origQty=0
        if orig is None or orig == 0:
            orig = requested_qty

        avg_price = _safe_float(data.get("avgPrice"))
        status = data.get("status", "UNKNOWN")
        if status not in VALID_STATUSES:
            status = "UNKNOWN"

        return StandardOrderResponse(
            exchange_order_id=str(data.get("orderId")) if data.get("orderId") is not None else None,
            client_order_id=data.get("clientOrderId"),
            symbol=data.get("symbol", ""),
            requested_qty=orig,
            filled_qty=executed,
            avg_price=avg_price,
            status=status,
            raw_response=data,
        )

    def _unknown_from(self, data: dict, requested_qty: float, reason: str) -> StandardOrderResponse:
        logger.critical(f"UNKNOWN: {reason}. Data: {data}")
        return StandardOrderResponse(
            exchange_order_id=None,
            client_order_id=None,
            symbol="",
            requested_qty=requested_qty,
            filled_qty=0.0,
            avg_price=None,
            status="UNKNOWN",
            raw_response=data,
        )

    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity: float, price: Optional[float],
                    client_order_id: str, intent: str) -> StandardOrderResponse:
        try:
            toobit_side = self._map_side(side, intent)
        except ValueError as e:
            return StandardOrderResponse(None, client_order_id, symbol, quantity, 0.0, None, "UNKNOWN", {"error": str(e)})

        params = {
            "symbol": symbol,
            "side": toobit_side,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
        }

        # v2.2: Market order — do NOT send `price` field at all
        if order_type == "MARKET":
            params["type"] = "LIMIT"
            params["priceType"] = "MARKET"
            # NO price field
        elif order_type == "LIMIT":
            params["type"] = "LIMIT"
            params["price"] = price
        else:
            return StandardOrderResponse(None, client_order_id, symbol, quantity, 0.0, None, "UNKNOWN",
                                         {"error": f"unsupported order_type: {order_type}"})

        data = self._request("POST", "/api/v1/futures/order", params)

        if not self._is_success(data):
            return StandardOrderResponse(None, client_order_id, symbol, quantity, 0.0, None, "UNKNOWN", data)

        return self._parse_order(data, quantity)

    def get_order_status(self, symbol: str, client_order_id: str) -> StandardOrderResponse:
        params = {"symbol": symbol, "origClientOrderId": client_order_id}
        data = self._request("GET", "/api/v1/futures/order", params)

        if not self._is_success(data):
            return StandardOrderResponse(None, client_order_id, symbol, 0.0, 0.0, None, "UNKNOWN", data)

        return self._parse_order(data, 0.0)