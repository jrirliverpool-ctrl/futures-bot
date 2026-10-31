
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

# Transitional states are valid but are not terminal.
VALID_STATUSES = (
    "PENDING_NEW",
    "NEW",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
    "FILLED",
    "CANCELED",
    "REJECTED",
)


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
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("TOOBIT_API_KEY")
        self.api_secret = api_secret or os.environ.get("TOOBIT_API_SECRET")

        if not self.api_key or not self.api_secret:
            raise ValueError("API credentials required")

    def _generate_signature(self, params: dict) -> str:
        sorted_items = sorted(params.items())
        query_string = urllib.parse.urlencode(sorted_items)

        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, method: str, path: str, params: dict) -> dict:
        """
        Perform exactly one HTTP request.

        A POST timeout or HTTP 5xx response is an ambiguous outcome.
        Never retry an order submission automatically.
        """

        if method not in ("GET", "POST"):
            raise ValueError(f"Unsupported method: {method}")

        url = f"{BASE_URL}{path}"
        headers = {"X-BB-APIKEY": self.api_key}

        # Avoid mutating caller-owned parameters.
        signed_params = dict(params or {})

        signed_params["timestamp"] = int(time.time() * 1000)
        signed_params["recvWindow"] = 5000
        signed_params["signature"] = self._generate_signature(
            signed_params
        )

        sorted_items = sorted(signed_params.items())

        try:
            if method == "GET":
                response = requests.get(
                    url,
                    params=sorted_items,
                    headers=headers,
                    timeout=15,
                )
            else:
                response = requests.post(
                    url,
                    data=sorted_items,
                    headers=headers,
                    timeout=15,
                )

        except requests.exceptions.Timeout:
            return {
                "_transport": "UNKNOWN",
                "error": "timeout; request outcome may be unknown",
            }

        except requests.exceptions.RequestException as exc:
            return {
                "_transport": "UNKNOWN",
                "error": (
                    "request exception; outcome may be unknown: "
                    f"{exc}"
                ),
            }

        status_code = getattr(response, "status_code", None)

        try:
            body = response.json()
        except ValueError:
            return {
                "_transport": "UNKNOWN",
                "_http_status": status_code,
                "error": (
                    f"non-JSON response (HTTP {status_code})"
                ),
            }

        if not isinstance(body, dict):
            return {
                "_transport": "UNKNOWN",
                "_http_status": status_code,
                "error": (
                    "unexpected JSON response type "
                    f"(HTTP {status_code})"
                ),
                "body": body,
            }

        if (
            not isinstance(status_code, int)
            or not 200 <= status_code < 300
        ):
            result = dict(body)

            if (
                not isinstance(status_code, int)
                or status_code >= 500
            ):
                result["_transport"] = "UNKNOWN"
            else:
                result["_transport"] = "HTTP_ERROR"

            result["_http_status"] = status_code
            return result

        result = dict(body)
        result["_http_status"] = status_code

        return result

    def _map_side(self, side: str, intent: str) -> str:
        if side == "LONG" and intent == "ENTRY":
            return "BUY_OPEN"

        if side == "LONG" and intent in ("EXIT", "TP1_HIT"):
            return "SELL_CLOSE"

        if side == "SHORT" and intent == "ENTRY":
            return "SELL_OPEN"

        if side == "SHORT" and intent in ("EXIT", "TP1_HIT"):
            return "BUY_CLOSE"

        raise ValueError(f"Invalid side/intent: {side}/{intent}")

    def _is_success(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return False

        if data.get("_transport"):
            return False

        http_status = data.get("_http_status")

        if (
            http_status is not None
            and not 200 <= http_status < 300
        ):
            return False

        if "orderId" in data or "status" in data:
            code = data.get("code")

            return code in (200, 0) if code is not None else True

        return data.get("code") in (200, 0)

    def _parse_order(
        self,
        raw_data: dict,
        requested_qty: float,
    ) -> StandardOrderResponse:

        data = (
            raw_data.get("data", raw_data)
            if isinstance(raw_data, dict)
            else {}
        )

        def safe_float(value):
            if isinstance(value, bool):
                return None

            try:
                number = float(value)

                if math.isfinite(number) and number >= 0:
                    return number

            except (ValueError, TypeError, OverflowError):
                pass

            return None

        executed = safe_float(data.get("executedQty"))
        original = safe_float(data.get("origQty"))

        if executed is None:
            return self._unknown_from(
                raw_data,
                requested_qty,
                "executedQty invalid",
            )

        if original is None or original == 0:
            original = requested_qty

        elif executed > original + 1e-8:
            return self._unknown_from(
                raw_data,
                requested_qty,
                "executedQty > origQty",
            )

        avg_price = safe_float(data.get("avgPrice"))

        status = data.get("status", "UNKNOWN")

        if status not in VALID_STATUSES:
            status = "UNKNOWN"

        return StandardOrderResponse(
            exchange_order_id=(
                str(data.get("orderId"))
                if data.get("orderId") is not None
                else None
            ),
            client_order_id=data.get("clientOrderId"),
            symbol=data.get("symbol", ""),
            requested_qty=original,
            filled_qty=executed,
            avg_price=avg_price,
            status=status,
            raw_response=raw_data,
        )

    def _unknown_from(
        self,
        data: dict,
        requested_qty: float,
        reason: str,
    ) -> StandardOrderResponse:

        logger.critical(
            "UNKNOWN: %s; response=%r",
            reason,
            data,
        )

        return StandardOrderResponse(
            None,
            None,
            "",
            requested_qty,
            0.0,
            None,
            "UNKNOWN",
            data,
        )

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        client_order_id: str,
        intent: str,
    ) -> StandardOrderResponse:

        try:
            toobit_side = self._map_side(side, intent)

        except ValueError as exc:
            return self._unknown_from(
                {"error": str(exc)},
                quantity,
                str(exc),
            )

        params = {
            "symbol": symbol,
            "side": toobit_side,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
        }

        if order_type == "MARKET":
            params["type"] = "LIMIT"
            params["priceType"] = "MARKET"

        elif order_type == "LIMIT":

            if price is None:
                return self._unknown_from(
                    {"error": "LIMIT price is required"},
                    quantity,
                    "bad LIMIT price",
                )

            params["type"] = "LIMIT"
            params["price"] = price

        else:
            return self._unknown_from(
                {"error": "bad order_type"},
                quantity,
                "bad order_type",
            )

        # Exactly one submission attempt.
        # Never retry an ambiguous POST automatically.
        data = self._request(
            "POST",
            "/api/v1/futures/order",
            params,
        )

        if not self._is_success(data):
            return self._unknown_from(
                data,
                quantity,
                "API_ERROR or ambiguous response",
            )

        return self._parse_order(data, quantity)

    def get_order_status(
        self,
        symbol: str,
        client_order_id: str,
    ) -> StandardOrderResponse:

        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
        }

        data = self._request(
            "GET",
            "/api/v1/futures/order",
            params,
        )

        if not self._is_success(data):
            return self._unknown_from(
                data,
                0.0,
                "query failed",
            )

        return self._parse_order(data, 0.0)
