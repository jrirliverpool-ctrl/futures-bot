import requests
import json

BASE_URL = "https://api.toobit.com"

# Sanity check
SANITY_ENDPOINT = "/quote/v1/klines"
SANITY_PARAMS = {"symbol": "BTC-SWAP-USDT", "interval": "1h", "limit": 5}

# ✅ endpoint صحیح طبق مستندات CCXT
EXCHANGE_INFO_ENDPOINT = "/api/v1/exchangeInfo"


def probe(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=15)
        return {
            "status": r.status_code,
            "body": r.text,
        }
    except requests.exceptions.RequestException as e:
        return {"status": "ERR", "error": str(e)}


print("=" * 70)
print("SANITY CHECK")
print("=" * 70)
sanity = probe(f"{BASE_URL}{SANITY_ENDPOINT}", SANITY_PARAMS)
print(f"Klines endpoint: {SANITY_ENDPOINT}")
print(f"Status: {sanity['status']}")
print(f"Preview: {sanity.get('body', '')[:200]}")
print()

if sanity["status"] != 200:
    print("❌ Sanity check failed.")
    exit(1)
print("✅ Sanity check passed.\n")

print("=" * 70)
print("FETCHING /api/v1/exchangeInfo")
print("=" * 70)

result = probe(f"{BASE_URL}{EXCHANGE_INFO_ENDPOINT}")

print(f"Status: {result['status']}")
print()

if result["status"] != 200:
    print(f"❌ exchangeInfo failed: {result.get('body', result.get('error'))[:500]}")
    exit(1)

# Parse JSON
try:
    data = json.loads(result["body"])
except json.JSONDecodeError:
    print("❌ Response is not valid JSON.")
    print(result["body"][:1000])
    exit(1)

# Save full response for inspection
with open("exchange_info_full.json", "w") as f:
    json.dump(data, f, indent=2)
print("💾 Full response saved to exchange_info_full.json\n")

# --- تحلیل ساختار ---
print("=" * 70)
print("TOP-LEVEL KEYS")
print("=" * 70)
if isinstance(data, dict):
    for key in data.keys():
        print(f"  • {key}: {type(data[key]).__name__}")
print()

# --- جستجوی BTC-SWAP-USDT ---
print("=" * 70)
print("SEARCHING FOR BTC-SWAP-USDT")
print("=" * 70)

symbols_key = None
for candidate in ["symbols", "data", "result", "contracts", "list"]:
    if isinstance(data, dict) and candidate in data:
        symbols_key = candidate
        break

if symbols_key is None and isinstance(data, dict):
    # Maybe the data itself is a dict of symbols
    for key, val in data.items():
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            symbols_key = key
            break

if symbols_key is None:
    print("⚠️ ساختار symbols پیدا نشد. در حال بررسی...")
    print(json.dumps(data, indent=2)[:3000])
    exit(1)

symbols = data[symbols_key]
print(f"✅ Found '{symbols_key}' with {len(symbols)} entries.\n")

# پیدا کردن BTC-SWAP-USDT
target = None
for s in symbols:
    if isinstance(s, dict) and s.get("symbol") == "BTC-SWAP-USDT":
        target = s
        break

if target is None:
    # Print first symbol as example
    print("⚠️ BTC-SWAP-USDT پیدا نشد. نمونه اولین symbol:")
    print(json.dumps(symbols[0], indent=2)[:1500])
    exit(1)

print("=" * 70)
print("🎯 BTC-SWAP-USDT SPECS (RAW)")
print("=" * 70)
print(json.dumps(target, indent=2))
print()

# --- جستجوی فیلدهای کلیدی ---
print("=" * 70)
print("KEY FIELDS DETECTED")
print("=" * 70)

FIELD_CANDIDATES = {
    "contract_multiplier": ["contractSize", "contractMultiplier", "multiplier", "contract_size", "size"],
    "min_qty": ["minQty", "minTradeNum", "minOrderQty", "min_qty", "minOrderAmount", "minTradeAmount"],
    "qty_step": ["stepSize", "qtyStep", "sizeStep", "quantityPrecision", "amountPrecision", "lotSize", "minTradeVolume"],
    "tick_size": ["tickSize", "priceStep", "priceEndStep", "pricePrecision", "minPriceIncrement"],
    "max_leverage": ["maxLeverage", "max_leverage", "leverage"],
    "price_precision": ["pricePrecision"],
    "quantity_precision": ["quantityPrecision"],
}

for label, keys in FIELD_CANDIDATES.items():
    found = None
    for k in keys:
        if k in target:
            found = (k, target[k])
            break
    marker = "✅" if found else "⚠️ "
    if found:
        print(f"{marker} {label}: {found[0]} = {found[1]}")
    else:
        print(f"{marker} {label}: (not found — needs mapping)")

print()
print("=" * 70)
print("✅ PROBE COMPLETED")
print("=" * 70)
print("→ فایل exchange_info_full.json را برای بررسی کامل نگه دار.")