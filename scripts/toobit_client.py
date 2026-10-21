"""
toobit_client.py — Secure API Client for Toobit Futures.
"""
import time
import hmac
import hashlib
import urllib.parse
import requests
import logging

class ToobitClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.toobit.com"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "X-BB-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        })
        self.logger = logging.getLogger("ToobitClient")

    def _generate_signature(self, params: dict) -> str:
        query_string = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _request(self, method: str, endpoint: str, params: dict = None):
        if params is None:
            params = {}
        
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = self._generate_signature(params)
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            if method.upper() == "GET":
                response = self.session.get(url, params=params, timeout=10)
            else:
                response = self.session.post(url, data=params, timeout=10)
                
            if response.status_code != 200:
                return {"error": f"HTTP {response.status_code}", "status": "NETWORK_ERROR"}
            
            data = response.json()
            if data.get("code") != 200 and data.get("code") != 0 and "code" in data:
                return {"error": data.get("msg", "Unknown API Error"), "status": "API_ERROR", "raw": data}
                
            return data
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network Error: {e}")
            return {"error": str(e), "status": "NETWORK_ERROR"}
        except ValueError as e:
            self.logger.error(f"Invalid JSON: {e}")
            return {"error": "Invalid JSON Response", "status": "NETWORK_ERROR"}

    def get_balance(self):
        return self._request("GET", "/api/v1/futures/balance")

    def get_position(self, symbol: str):
        return self._request("GET", "/api/v1/futures/position", {"symbol": symbol})

    def place_order(self, symbol: str, side: str, order_type: str, quantity: float, price: float = None, client_order_id: str = None):
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": str(quantity)
        }
        if price:
            params["price"] = str(price)
        if client_order_id:
            params["newClientOrderId"] = client_order_id
            
        return self._request("POST", "/api/v1/futures/order", params)

    def get_order_status(self, symbol: str, client_order_id: str):
        return self._request("GET", "/api/v1/futures/order", {"symbol": symbol, "origClientOrderId": client_order_id})