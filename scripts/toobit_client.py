"""
toobit_client.py — Strict Read-Only Toobit Futures Client.
No order submission; sanitized errors; deterministic signatures.
"""
import hashlib
import hmac
import time
from urllib.parse import urlencode, urlsplit

import requests

class ToobitClient:
    BASE_URL = "https://api.toobit.com"
    BALANCE_ENDPOINT = "/api/v1/futures/balance"

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = BASE_URL):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("API key is required")
        if not isinstance(api_secret, str) or not api_secret:
            raise ValueError("API secret is required")

        # Prevent accidental credential transmission to another host.
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.toobit.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Unapproved API base URL")

        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = self.BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "X-BB-APIKEY": api_key,
        })

    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        timestamp: int | None = None,
    ) -> dict:
        # Fail closed: this client never submits POST/PUT/DELETE requests.
        if method.upper() != "GET":
            return {"status": "REJECTED"}

        # Only allow the verified read-only endpoint for now.
        if endpoint != self.BALANCE_ENDPOINT:
            return {"status": "REJECTED"}

        if params is not None and not isinstance(params, dict):
            return {"status": "INVALID_REQUEST"}

        query_params = dict(params or {})

        # Prevent caller-controlled overrides of security parameters.
        if "timestamp" in query_params or "recvWindow" in query_params:
            return {"status": "INVALID_REQUEST"}

        if timestamp is None:
            timestamp = int(time.time() * 1000)

        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            return {"status": "INVALID_REQUEST"}

        query_params["timestamp"] = timestamp
        query_params["recvWindow"] = 5000

        query_string = urlencode(sorted(query_params.items()))
        signature = self._generate_signature(query_string)

        url = (
            f"{self.base_url}{endpoint}"
            f"?{query_string}&signature={signature}"
        )

        try:
            response = self.session.get(url, timeout=10)
        except requests.exceptions.Timeout:
            return {"status": "NETWORK_ERROR", "error": "TIMEOUT"}
        except requests.exceptions.RequestException:
            # Do not expose exception text, request URLs, or credentials.
            return {"status": "NETWORK_ERROR", "error": "REQUEST_FAILED"}

        if response.status_code != 200:
            return {
                "status": "HTTP_ERROR",
                "status_code": response.status_code,
            }

        try:
            data = response.json()
        except ValueError:
            return {
                "status": "INVALID_RESPONSE",
                "error": "INVALID_JSON",
            }

        # Balance endpoint normally returns a JSON array.
        # If the server returns an error object, don't treat it as success.
        if isinstance(data, dict) and "code" in data:
            if data["code"] not in (200, 0):
                return {
                    "status": "API_ERROR",
                    "api_code": str(data["code"])[:32],
                }

        if not isinstance(data, (dict, list)):
            return {"status": "INVALID_RESPONSE"}

        return {"status": "SUCCESS", "data": data}

    def get_balance(self, timestamp=None):
        return self._request(
            "GET",
            self.BALANCE_ENDPOINT,
            timestamp=timestamp,
        )