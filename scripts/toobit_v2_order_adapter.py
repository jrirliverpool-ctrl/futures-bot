"""Isolated Toobit USDT-M V2 order adapter.

This adapter is intentionally NOT wired into ExecutionManager.
It supports deterministic request construction and read-only test coverage,
but it does not make the project live-trading ready.

Safety properties:
- Uses the documented V2 JSON order endpoint and DECIMAL contract quantity.
- Signs the exact compact JSON body sent over the wire.
- Makes exactly one POST attempt; no retry after an ambiguous outcome.
- Converts any ambiguous response to status UNKNOWN.
- Requires the response identity (symbol/client id/side/positionSide/qty)
  to match the submitted intent before returning a known order state.
- Does not change leverage, margin mode, or any account setting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal, InvalidOperation

import requests


BASE_URL = "https://api.toobit.com"
ORDER_PATH = "/api/v2/futures/order"
VALID_ORDER_TYPES = {"MARKET", "LIMIT"}
VALID_TIME_IN_FORCE = {"GTC", "FOK", "IOC", "POST_ONLY"}


class ToobitV2OrderAdapter:
    """One-shot V2 order request wrapper around an existing ToobitClient."""

    def __init__(
        self,
        client,
        timeout: int = 15,
        allow_network_submission: bool = False,
    ):
        if client is None:
            raise ValueError("client is required")
        if not isinstance(getattr(client, "api_key", None), str) or not client.api_key:
            raise ValueError("client API key is required")
        if not isinstance(getattr(client, "api_secret", None), str) or not client.api_secret:
            raise ValueError("client API secret is required")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        for method in ("_parse_order", "_unknown_from", "_v2_order_side"):
            if not callable(getattr(client, method, None)):
                raise ValueError(f"client is missing {method}")

        if not isinstance(allow_network_submission, bool):
            raise ValueError("allow_network_submission must be bool")

        self.client = client
        self.timeout = timeout
        self.allow_network_submission = allow_network_submission

    @staticmethod
    def _decimal_string(value, field: str) -> str:
        if value is None or isinstance(value, bool):
            raise ValueError(f"{field} must be a positive decimal")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"{field} must be numeric") from None
        if not number.is_finite() or number <= 0:
            raise ValueError(f"{field} must be finite and > 0")
        # Fixed-point formatting avoids scientific notation in API payloads.
        return format(number, "f")

    @staticmethod
    def _map_v2_side(side: str, intent: str) -> tuple[str, str, str]:
        mapping = {
            ("LONG", "ENTRY"): ("BUY", "LONG", "BUY_OPEN"),
            ("LONG", "EXIT"): ("SELL", "LONG", "SELL_CLOSE"),
            ("LONG", "TP1_HIT"): ("SELL", "LONG", "SELL_CLOSE"),
            ("SHORT", "ENTRY"): ("SELL", "SHORT", "SELL_OPEN"),
            ("SHORT", "EXIT"): ("BUY", "SHORT", "BUY_CLOSE"),
            ("SHORT", "TP1_HIT"): ("BUY", "SHORT", "BUY_CLOSE"),
        }
        if side not in ("LONG", "SHORT") or intent not in ("ENTRY", "EXIT", "TP1_HIT"):
            raise ValueError(f"invalid side/intent: {side}/{intent}")
        return mapping[(side, intent)]

    def _unknown(self, payload, requested_qty: float, reason: str):
        return self.client._unknown_from(payload, requested_qty, reason)

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity,
        price=None,
        client_order_id: str,
        intent: str,
        time_in_force: str | None = None,
    ):
        """Submit one V2 order; callers must handle UNKNOWN as unresolved.

        This method validates shape but deliberately does not enforce strategy,
        portfolio risk, exchange minimum notional, or account protection. Those
        checks belong upstream and are not bypassed by this adapter.
        """
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("client_order_id is required")
        if not isinstance(order_type, str) or order_type not in VALID_ORDER_TYPES:
            raise ValueError("order_type must be MARKET or LIMIT")

        qty_text = self._decimal_string(quantity, "quantity")
        qty_decimal = Decimal(qty_text)
        try:
            requested_qty = float(qty_decimal)
        except (OverflowError, ValueError):
            raise ValueError("quantity cannot be represented") from None
        if requested_qty <= 0 or requested_qty == float("inf"):
            raise ValueError("quantity cannot be represented as a finite float")

        v2_side, position_side, legacy_side = self._map_v2_side(side, intent)

        body = {
            "symbol": symbol,
            "side": v2_side,
            "positionSide": position_side,
            "type": order_type,
            "newClientOrderId": client_order_id,
            "quantity": qty_text,
        }

        if order_type == "LIMIT":
            body["price"] = self._decimal_string(price, "price")
            tif = "GTC" if time_in_force is None else time_in_force
            if not isinstance(tif, str) or tif not in VALID_TIME_IN_FORCE:
                raise ValueError("invalid time_in_force")
            body["timeInForce"] = tif
        else:
            if price is not None:
                raise ValueError("MARKET order must omit price")
            if time_in_force is not None:
                raise ValueError("MARKET order must omit time_in_force")

        # Toobit V2 JSON-body signature: query string + exact compact body.
        query = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
            "category": "USDT",
        }
        query_string = urllib.parse.urlencode(list(query.items()))
        raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        signing_payload = query_string + raw_body
        signature = hmac.new(
            self.client.api_secret.encode("utf-8"),
            signing_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = (
            f"{BASE_URL}{ORDER_PATH}?{query_string}"
            f"&signature={signature}"
        )
        headers = {
            "X-BB-APIKEY": self.client.api_key,
            "Content-Type": "application/json",
        }

        # Network submission is opt-in and disabled by default. This adapter
        # must not become an accidental live-order path when imported or tested.
        if not self.allow_network_submission:
            raise PermissionError(
                "V2 order submission is disabled; explicit opt-in is required"
            )

        # Exactly one POST: a timeout or connection drop may mean the exchange
        # accepted the order. Never retry automatically or fall back to V1.
        try:
            response = requests.post(
                url,
                headers=headers,
                data=raw_body,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            return self._unknown(
                {"_transport": "UNKNOWN", "error": "V2 order POST timed out; outcome unknown"},
                requested_qty,
                "V2 order submission timed out; exchange outcome is unknown",
            )
        except requests.exceptions.RequestException as exc:
            return self._unknown(
                {
                    "_transport": "UNKNOWN",
                    "error": f"V2 order POST transport error ({type(exc).__name__})",
                },
                requested_qty,
                "V2 order submission transport outcome is unknown",
            )

        status_code = getattr(response, "status_code", None)
        try:
            envelope = response.json()
        except ValueError:
            return self._unknown(
                {
                    "_transport": "UNKNOWN",
                    "_http_status": status_code,
                    "error": "V2 order response is not valid JSON",
                },
                requested_qty,
                "V2 order response is not valid JSON",
            )

        if not isinstance(envelope, dict):
            return self._unknown(
                {"_transport": "UNKNOWN", "_http_status": status_code, "body_type": type(envelope).__name__},
                requested_qty,
                "V2 order response envelope is invalid",
            )

        envelope = dict(envelope)
        envelope["_http_status"] = status_code

        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            envelope["_transport"] = "UNKNOWN" if not isinstance(status_code, int) or status_code >= 500 else "HTTP_ERROR"
            return self._unknown(envelope, requested_qty, "V2 order HTTP response is not successful")

        if envelope.get("code") != 200:
            return self._unknown(envelope, requested_qty, "V2 order API did not confirm acceptance")

        data = envelope.get("data")
        if not isinstance(data, dict):
            return self._unknown(envelope, requested_qty, "V2 order response data is not an object")

        # The API's acknowledgement must identify this exact request. If it
        # does not, preserve UNKNOWN so recovery can inspect the journal.
        if data.get("symbol") != symbol:
            return self._unknown(envelope, requested_qty, "V2 order response symbol mismatch")
        if data.get("clientOrderId") != client_order_id:
            return self._unknown(envelope, requested_qty, "V2 order response client id mismatch")
        if self.client._v2_order_side(data) != legacy_side:
            return self._unknown(envelope, requested_qty, "V2 order response side mismatch")
        if data.get("type") != order_type:
            return self._unknown(envelope, requested_qty, "V2 order response type mismatch")
        if data.get("orderId") is None:
            return self._unknown(envelope, requested_qty, "V2 order response missing order id")

        try:
            returned_qty = Decimal(str(data.get("origQty")))
        except (InvalidOperation, TypeError, ValueError):
            return self._unknown(envelope, requested_qty, "V2 order response quantity invalid")
        if not returned_qty.is_finite() or returned_qty != qty_decimal:
            return self._unknown(envelope, requested_qty, "V2 order response quantity mismatch")

        return self.client._parse_order(envelope, requested_qty)