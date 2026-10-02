import requests
import json

BASE_URL = "https://api.toobit.com"

# Sanity check
SANITY_ENDPOINT = "/quote/v1/klines"
SANITY_PARAMS = {"symbol": "BTC-SWAP-USDT", "interval": "1h", "limit": 5}

# کاندیدهای endpoint مشخصات قرارداد
CANDIDATES = [
    "/api/v1/futures/exchangeInfo",
    "/api/v2/mix/market/contracts",
    "/api/v1/mix/market/contracts",
    "/api/v1/futures/contracts",
    "/api/v1/futures/instruments",
    "/api/v1/futures/symbols",
    "/api/v1/market/contracts",
    "/api/v1/futures/public/contracts",
]

CANDIDATE_PARAMS = [
    {},
    {"symbol": "BTC-SWAP-USDT"},
    {"productType": "USDT-FUTURES"},
]


def probe(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        return {
            "status": r.status_code,
            "body_preview": r.text[:500] if r.text else "",
        }
    except requests.exceptions.RequestException as e:
        return {"status": "ERR", "error": str(e)}


print("=" * 70)
print("SANITY CHECK")
print("=" * 70)

sanity = probe(f"{BASE_URL}{SANITY_ENDPOINT}", SANITY_PARAMS)
print(f"Endpoint: {SANITY_ENDPOINT}")
print(f"Status: {sanity['status']}")
print(f"Preview: {sanity.get('body_preview', 'N/A')[:200]}")
print()

if sanity["status"] != 200:
    print("Sanity check failed.")
    exit(1)

print("=" * 70)
print("PROBING - Searching for contract specs endpoint")
print("=" * 70)

results = []

for endpoint in CANDIDATES:
    for params in CANDIDATE_PARAMS:
        url = f"{BASE_URL}{endpoint}"
        params_str = json.dumps(params) if params else "{}"

        result = probe(url, params)
        status = result.get("status")
        preview = result.get("body_preview", "")[:200]

        marker = "[OK]" if status == 200 else "    "

        print(f"{marker} {endpoint}")
        print(f"     params: {params_str}")
        print(f"     status: {status}")
        if status == 200:
            print(f"     preview: {preview}")
        print()

        results.append({
            "endpoint": endpoint,
            "params": params,
            "status": status,
            "preview": preview,
        })

print("=" * 70)
print("SUMMARY")
print("=" * 70)

working = [r for r in results if r["status"] == 200]

if not working:
    print("No endpoint returned 200.")
    exit(1)

print(f"{len(working)} working endpoint(s):")
print()
for r in working:
    print(f"   - {r['endpoint']}")
    print(f"     params: {json.dumps(r['params'])}")
    print(f"     preview: {r['preview'][:150]}")
    print()

print("Now we can extract exact BTC-SWAP-USDT specs from these endpoints.")