"""
toobit_client.py — Secure Read-Only API Client for Toobit Futures.
v1.2 - Read-Only, strict error handling, injectable timestamp for testing.
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

    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _request(self, method: str, endpoint: str, params: dict = None, timestamp: int = None):
        if params is None:
            params = {}
        
        if timestamp is None:
            timestamp = int(time.time() * 1000)
            
        params["timestamp"] = timestamp
        params["recvWindow"] = 5000
        
        # مرتب‌سازی و ساخت Query String
        query_string = urllib.parse.urlencode(sorted(params.items()))
        signature = self._generate_signature(query_string)
        
        final_url = f"{self.base_url}{endpoint}?{query_string}&signature={signature}"
        
        try:
            if method.upper() == "GET":
                response = self.session.get(final_url, timeout=10)
            else:
                response = self.session.post(final_url, timeout=10)
                
            # 1. خطای شبکه (HTTP Status)
            if response.status_code != 200:
                return {
                    "error": f"HTTP {response.status_code}",
                    "status": "HTTP_ERROR",
                    "status_code": response.status_code,
                    "raw": response.text
                }
            
            # 2. خطای JSON
            try:
                data = response.json()
            except ValueError:
                return {"error": "Invalid JSON response", "status": "NETWORK_ERROR", "raw": response.text}
            
            # 3. خطای تجاری (API Error)
            if isinstance(data, dict) and "code" in data:
                if data.get("code") not in (200, 0):
                    return {"error": data.get("msg", "API Error"), "status": "API_ERROR", "raw": data}
                
            return {"status": "SUCCESS", "data": data}
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network Error: {e}")
            return {"error": str(e), "status": "NETWORK_ERROR"}

    def get_balance(self, timestamp=None):
        """فقط خواندن موجودی (بدون امکان ارسال سفارش)"""
        return self._request("GET", "/api/v1/futures/balance", timestamp=timestamp)

    def get_position(self, symbol: str, timestamp=None):
        return self._request("GET", "/api/v1/futures/position", {"symbol": symbol}, timestamp=timestamp)