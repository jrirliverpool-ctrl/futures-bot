import os
import time
import hmac
import hashlib
import requests
import json

API_KEY = os.environ.get("TOOBIT_API_KEY")
API_SECRET = os.environ.get("TOOBIT_API_SECRET")

BASE_URL = "https://api.toobit.com"

def sign(query_string: str) -> str:
    return hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def private_get(endpoint: str, params: dict = None):
    if params is None: params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    sorted_params = sorted(params.items())
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    signature = sign(query_string)
    headers = {"X-BB-APIKEY": API_KEY}
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}

# --- دریافت اطلاعات ---
bal = private_get("/api/v1/futures/balance")
pos = private_get("/api/v1/futures/positions")

# --- ساخت دیکشنری وضعیت ---
state = {
    "timestamp": int(time.time() * 1000),
    "balance": bal,
    "positions": pos
}

# --- ذخیره در فایل state.json ---
with open("state.json", "w") as f:
    json.dump(state, f, indent=4)

print("✅ State saved to state.json")
print(json.dumps(state, indent=2))