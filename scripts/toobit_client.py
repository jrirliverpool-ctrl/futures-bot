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

VALID_STATUSES = (
    "PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL",
    "FILLED", "CANCELED", "REJECTED",
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
    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key or os.environ.get("TOOBIT_API_KEY")
        self.api_secret = api_secret or os.environ.get("TOOBIT_API_SECRET")
        if not self.api_key or not self.api_secret:
            raise ValueError("API credentials required")

    def _generate_signature(self, params):
        query = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, method, path, params):
        """Legacy form/query signed request. Exactly one HTTP attempt."""
        if method not in ("GET", "POST"):
            raise ValueError(f"Unsupported method: {method}")

        url = f"{BASE_URL}{path}"
        headers = {"X-BB-APIKEY": self.api_key}
        signed = dict(params or {})
        signed["timestamp"] = int(time.time() * 1000)
        signed["recvWindow"] = 5000
        signed["signature"] = self._generate_signature(signed)
        items = sorted(signed.items())

        try:
            if method == "GET":
                response = requests.get(
                    url, params=items, headers=headers, timeout=15
                )
            else:
                response = requests.post(
                    url, data=items, headers=headers, timeout=15
                )
        except requests.exceptions.Timeout:
            return {
                "_transport": "UNKNOWN",
                "error": "timeout; request outcome may be unknown",
            }
        except requests.exceptions.RequestException as exc:
            return {
                "_transport": "UNKNOWN",
                "error": f"request exception ({type(exc).__name__})",
            }

        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except ValueError:
            return {
                "_transport": "UNKNOWN",
                "_http_status": status_code,
                "error": f"non-JSON response (HTTP {status_code})",
            }

        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            result = dict(body) if isinstance(body, dict) else {"body": body}
            result["_transport"] = (
                "UNKNOWN"
                if not isinstance(status_code, int) or status_code >= 500
                else "HTTP_ERROR"
            )
            result["_http_status"] = status_code
            return result

        if isinstance(body, list):
            if not all(isinstance(x, dict) for x in body):
                return {
                    "_transport": "UNKNOWN",
                    "_http_status": status_code,
                    "error": "unexpected JSON array item type",
                }
            return body

        if not isinstance(body, dict):
            return {
                "_transport": "UNKNOWN",
                "_http_status": status_code,
                "error": f"unexpected JSON response type (HTTP {status_code})",
            }

        result = dict(body)
        result["_http_status"] = status_code
        return result

    def _request_v2_get(self, path, params):
        """Signed V2 GET request; query string is the signed payload."""
        if not path.startswith("/api/v2/"):
            raise ValueError("V2 request path required")

        signed = dict(params or {})
        signed["timestamp"] = int(time.time() * 1000)
        signed["recvWindow"] = 5000

        # Toobit requires request parameter order to match signature order.
        ordered = sorted(signed.items())
        query = urllib.parse.urlencode(ordered)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{BASE_URL}{path}"
        headers = {"X-BB-APIKEY": self.api_key}

        try:
            response = requests.get(
                url,
                params=ordered + [("signature", signature)],
                headers=headers,
                timeout=15,
            )
        except requests.exceptions.Timeout:
            raise RuntimeError("V2 request timed out; result is unknown")
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"V2 request failed ({type(exc).__name__}); result is unknown"
            )

        status = getattr(response, "status_code", None)
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"V2 returned non-JSON response (HTTP {status})")

        if not isinstance(status, int) or not 200 <= status < 300:
            code = body.get("code") if isinstance(body, dict) else None
            raise RuntimeError(
                f"V2 HTTP error (status={status}, code={str(code)[:32]})"
            )

        if not isinstance(body, dict):
            raise RuntimeError("V2 response envelope is not an object")

        if body.get("code") != 200:
            code = body.get("code")
            raise RuntimeError(
                f"V2 API rejected request (code={str(code)[:32]})"
            )

        if "data" not in body:
            raise RuntimeError("V2 response is missing data")

        return body["data"]

    def _request_v2_post(self, path, params):
        """Submit one signed V2 form request exactly once.

        A transport failure is ambiguous for TRADE routes. Callers must treat
        it as UNKNOWN and reconcile by client order id before taking action.
        """
        if not path.startswith("/api/v2/"):
            raise ValueError("V2 request path required")

        signed = dict(params or {})
        signed["timestamp"] = int(time.time() * 1000)
        signed["recvWindow"] = 5000
        ordered = sorted(signed.items())
        query = urllib.parse.urlencode(ordered)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            response = requests.post(
                f"{BASE_URL}{path}",
                data=ordered + [("signature", signature)],
                headers={"X-BB-APIKEY": self.api_key},
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"V2 POST outcome is unknown ({type(exc).__name__})"
            ) from None

        status = getattr(response, "status_code", None)
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(
                f"V2 POST returned non-JSON response (HTTP {status}); outcome is unknown"
            ) from None

        if not isinstance(status, int) or not 200 <= status < 300:
            code = body.get("code") if isinstance(body, dict) else None
            raise RuntimeError(
                f"V2 POST HTTP error (status={status}, code={str(code)[:32]})"
            )
        if not isinstance(body, dict):
            raise RuntimeError("V2 POST response envelope is not an object")
        if body.get("code") != 200:
            code = body.get("code")
            raise RuntimeError(
                f"V2 POST API rejected request (code={str(code)[:32]})"
            )
        if "data" not in body:
            raise RuntimeError("V2 POST response is missing data")
        return body["data"]

    @staticmethod
    def _map_side(side, intent):
        mapping = {
            ("LONG", "ENTRY"): "BUY_OPEN",
            ("LONG", "EXIT"): "SELL_CLOSE",
            ("LONG", "TP1_HIT"): "SELL_CLOSE",
            ("SHORT", "ENTRY"): "SELL_OPEN",
            ("SHORT", "EXIT"): "BUY_CLOSE",
            ("SHORT", "TP1_HIT"): "BUY_CLOSE",
        }
        try:
            return mapping[(side, intent)]
        except KeyError:
            raise ValueError(f"Invalid side/intent: {side}/{intent}")

    @staticmethod
    def _v2_order_side(data):
        side = data.get("side")
        position_side = data.get("positionSide")
        mapping = {
            ("BUY", "LONG"): "BUY_OPEN",
            ("SELL", "SHORT"): "SELL_OPEN",
            ("SELL", "LONG"): "SELL_CLOSE",
            ("BUY", "SHORT"): "BUY_CLOSE",
        }
        return mapping.get((side, position_side))

    @staticmethod
    def _is_success(data):
        if not isinstance(data, dict) or data.get("_transport"):
            return False
        http_status = data.get("_http_status")
        if http_status is not None and not 200 <= http_status < 300:
            return False
        if "orderId" in data or "status" in data:
            code = data.get("code")
            return code in (200, 0) if code is not None else True
        return data.get("code") in (200, 0)

    def _parse_order(self, raw_data, requested_qty):
        data = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else {}

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
            return self._unknown_from(raw_data, requested_qty, "executedQty invalid")

        if original is None or original == 0:
            original = requested_qty
        elif executed > original + 1e-8:
            return self._unknown_from(raw_data, requested_qty, "executedQty > origQty")

        avg_price = safe_float(data.get("avgPrice"))
        status = data.get("status", "UNKNOWN")
        if status not in VALID_STATUSES:
            status = "UNKNOWN"

        return StandardOrderResponse(
            exchange_order_id=(
                str(data["orderId"]) if data.get("orderId") is not None else None
            ),
            client_order_id=data.get("clientOrderId"),
            symbol=data.get("symbol", ""),
            requested_qty=original,
            filled_qty=executed,
            avg_price=avg_price,
            status=status,
            raw_response=raw_data,
        )

    def _unknown_from(self, data, requested_qty, reason):
        metadata = {}
        if isinstance(data, dict):
            for key in ("_http_status", "_transport", "code"):
                if data.get(key) is not None:
                    metadata[key] = str(data[key])[:32]
        logger.critical(
            "UNKNOWN: %s; safe_metadata=%s",
            str(reason)[:160],
            metadata,
        )
        return StandardOrderResponse(
            None, None, "", requested_qty, 0.0, None, "UNKNOWN", data
        )

    # ------------------------------------------------------------------
    # Legacy V1 methods. Kept for existing callers and regression tests.
    # ------------------------------------------------------------------

    def place_order(
        self, symbol, side, order_type, quantity, price,
        client_order_id, intent
    ):
        try:
            toobit_side = self._map_side(side, intent)
        except ValueError as exc:
            return self._unknown_from({"error": str(exc)}, quantity, str(exc))

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
                    {"error": "LIMIT price is required"}, quantity, "bad LIMIT price"
                )
            params["type"] = "LIMIT"
            params["price"] = price
        else:
            return self._unknown_from(
                {"error": "bad order_type"}, quantity, "bad order_type"
            )

        data = self._request("POST", "/api/v1/futures/order", params)
        if not self._is_success(data):
            return self._unknown_from(data, quantity, "API_ERROR or ambiguous response")
        return self._parse_order(data, quantity)

    def get_order_status(self, symbol, client_order_id):
        params = {"symbol": symbol, "origClientOrderId": client_order_id}
        data = self._request("GET", "/api/v1/futures/order", params)
        if not self._is_success(data):
            return self._unknown_from(data, 0.0, "query failed")
        return self._parse_order(data, 0.0)

    def get_user_trades(
        self, symbol, start_time=None, end_time=None,
        limit=1000, from_id=None, to_id=None
    ):
        """Legacy V1 read-only endpoint."""
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")

        params = {"symbol": symbol, "limit": limit}
        for name, value in (
            ("startTime", start_time), ("endTime", end_time),
            ("fromId", from_id), ("toId", to_id),
        ):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
                params[name] = value

        if start_time is not None and end_time is not None and end_time < start_time:
            raise ValueError("end_time must be >= start_time")

        data = self._request("GET", "/api/v1/futures/userTrades", params)
        if not isinstance(data, list):
            transport = data.get("_transport", "API_ERROR") if isinstance(data, dict) else "API_ERROR"
            status = data.get("_http_status") if isinstance(data, dict) else None
            code = data.get("code") if isinstance(data, dict) else None
            raise RuntimeError(
                f"userTrades V1 failed (transport={str(transport)[:32]}, "
                f"http_status={status}, code={str(code)[:32]})"
            )
        return data

    # ------------------------------------------------------------------
    # V2 order and read-only methods.
    # ------------------------------------------------------------------

    def place_order_v2(
        self, symbol, side, order_type, quantity, price,
        client_order_id, intent, time_in_force="GTC"
    ):
        """Place one USDT-M V2 order with an explicit hedge position side.

        This low-level method does not perform portfolio-risk, contract-step,
        account-mode, or protection-order preflight. A caller must not retry an
        UNKNOWN result; first query it with get_order_status_v2.
        """
        requested_qty = 0.0

        def invalid(reason):
            return self._unknown_from({"error": reason}, requested_qty, reason)

        if not isinstance(symbol, str) or not symbol.strip():
            return invalid("symbol is required")
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            return invalid("client_order_id is required")
        if time_in_force not in ("GTC", "FOK", "IOC", "POST_ONLY"):
            return invalid("invalid time_in_force")
        if isinstance(quantity, bool):
            return invalid("quantity must be finite and positive")
        try:
            numeric_qty = float(quantity)
        except (TypeError, ValueError, OverflowError):
            return invalid("quantity must be finite and positive")
        if not math.isfinite(numeric_qty) or numeric_qty <= 0:
            return invalid("quantity must be finite and positive")
        requested_qty = numeric_qty

        try:
            legacy_side = self._map_side(side, intent)
        except ValueError as exc:
            return invalid(str(exc))
        v2_side, position_side = {
            "BUY_OPEN": ("BUY", "LONG"),
            "SELL_OPEN": ("SELL", "SHORT"),
            "BUY_CLOSE": ("BUY", "SHORT"),
            "SELL_CLOSE": ("SELL", "LONG"),
        }[legacy_side]

        if order_type not in ("MARKET", "LIMIT"):
            return invalid("invalid order_type")
        params = {
            "symbol": symbol,
            "side": v2_side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
            "category": "USDT",
        }
        if order_type == "LIMIT":
            if isinstance(price, bool):
                return invalid("LIMIT price must be finite and positive")
            try:
                numeric_price = float(price)
            except (TypeError, ValueError, OverflowError):
                return invalid("LIMIT price must be finite and positive")
            if not math.isfinite(numeric_price) or numeric_price <= 0:
                return invalid("LIMIT price must be finite and positive")
            params["price"] = price
            params["timeInForce"] = time_in_force
        elif price is not None:
            return invalid("MARKET order must not include price")

        try:
            data = self._request_v2_post("/api/v2/futures/order", params)
        except (RuntimeError, requests.exceptions.RequestException) as exc:
            return self._unknown_from(
                {"_transport": "UNKNOWN", "error": str(exc)[:160]},
                requested_qty,
                "V2 order submission outcome is not confirmed",
            )

        if not isinstance(data, dict):
            return self._unknown_from(
                {"error": "V2 order data is not an object"},
                requested_qty,
                "invalid V2 order response",
            )
        if data.get("symbol") != symbol or self._v2_order_side(data) != legacy_side:
            return self._unknown_from(
                {"error": "V2 order response symbol or side mismatch"},
                requested_qty,
                "V2 order response mismatch",
            )
        return self._parse_order({"data": data}, requested_qty)

    def get_order_status_v2(self, symbol, client_order_id):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("client_order_id is required")

        data = self._request_v2_get(
            "/api/v2/futures/order",
            {
                "origClientOrderId": client_order_id,
                "category": "USDT",
            },
        )
        if not isinstance(data, dict):
            return self._unknown_from(
                {"error": "V2 order data is not an object"}, 0.0,
                "invalid V2 order response",
            )
        if data.get("symbol") != symbol:
            return self._unknown_from(
                {"error": "V2 order symbol mismatch"}, 0.0,
                "V2 order symbol mismatch",
            )
        if self._v2_order_side(data) is None:
            return self._unknown_from(
                {"error": "V2 order side/positionSide invalid"}, 0.0,
                "invalid V2 order side",
            )
        return self._parse_order({"data": data}, 0.0)

    def get_open_algo_orders_v2(
        self, *, stop_category, symbol=None, order_id=None, limit=1000
    ):
        """Read one page of conditional / protection orders from V2."""
        if stop_category not in ("STOP", "STOP_PROFIT_LOSS"):
            raise ValueError("invalid stop_category")
        if symbol is not None and (
            not isinstance(symbol, str) or not symbol.strip()
        ):
            raise ValueError("symbol must be non-empty text")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be from 1 to 1000")
        if order_id is not None and (
            isinstance(order_id, bool)
            or not str(order_id).isdigit()
            or int(order_id) <= 0
        ):
            raise ValueError("order_id must be a positive integer")

        params = {
            "stopCategory": stop_category,
            "category": "USDT",
            "limit": limit,
        }
        if symbol is not None:
            params["symbol"] = symbol
        if order_id is not None:
            params["orderId"] = int(order_id)
        data = self._request_v2_get(
            "/api/v2/futures/open-algo-orders", params
        )
        if not isinstance(data, list) or not all(
            isinstance(row, dict) for row in data
        ):
            raise RuntimeError("V2 open algo orders data is not an object array")
        return data

    def get_user_trades_v2(
        self, symbol, start_time=None, end_time=None,
        limit=1000, from_id=None, to_id=None
    ):
        """Read one page from V2 user-trades. Does not claim pagination complete."""
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")

        params = {
            "symbol": symbol,
            "category": "USDT",
            "limit": limit,
        }
        for name, value in (
            ("startTime", start_time), ("endTime", end_time),
            ("fromId", from_id), ("toId", to_id),
        ):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
                params[name] = value

        if start_time is not None and end_time is not None and end_time < start_time:
            raise ValueError("end_time must be >= start_time")

        data = self._request_v2_get("/api/v2/futures/user-trades", params)
        if not isinstance(data, list):
            raise RuntimeError("V2 user-trades data is not an array")
        if not all(isinstance(row, dict) for row in data):
            raise RuntimeError("V2 user-trades contains a non-object row")
        return data
