import os
import time
import hmac
import hashlib
import requests

# خواندن از GitHub Secrets
API_KEY = os.environ.get("TOOBIT_API_KEY")
API_SECRET = os.environ.get("TOOBIT_API_SECRET")

BASE_URL = "https://api.toobit.com"

def sign(query_string: str) -> str:
    return hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def private_get(endpoint: str, params: dict = None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Toobit read-only API keys are not configured")
    if params is None:
        params = {}
    
    # اضافه کردن پارامترهای اجباری
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    
    # نکته کلیدی: مرتب‌سازی پارامترها بر اساس حروف الفبا (a-z)
    sorted_params = sorted(params.items())
    
    # ساخت query string بر اساس ترتیب مرتب‌شده
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    
    # امضا
    signature = sign(query_string)
    
    # هدر صحیح بر اساس مستندات
    headers = {
        "X-BB-APIKEY": API_KEY,
    }
    
    # آدرس نهایی با امضا در انتهای query string
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}

def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("Toobit read-only API keys are not configured")

    checks = (
        ("Futures Balance", "/api/v1/futures/balance"),
        ("Positions", "/api/v1/futures/positions"),
    )
    results = []
    print("Running read-only Toobit account checks...")
    for name, endpoint in checks:
        try:
            response = private_get(endpoint)
            ok = not (isinstance(response, dict) and "error" in response)
        except Exception:
            # Never print raw account data, request URLs, or exception contents.
            ok = False
        results.append((name, ok))
        print(f"{'PASS' if ok else 'FAIL'}: {name}")

    passed = sum(ok for _, ok in results)
    print(f"Result: {passed}/{len(results)} read-only checks passed")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
