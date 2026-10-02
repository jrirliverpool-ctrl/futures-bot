import requests
import json

BASE_URL = "https://api.toobit.com"
EXCHANGE_INFO_ENDPOINT = "/api/v1/exchangeInfo"


def probe(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=15)
        return {"status": r.status_code, "body": r.text}
    except requests.exceptions.RequestException as e:
        return {"status": "ERR", "error": str(e)}


print("=" * 70)
print("FETCHING /api/v1/exchangeInfo")
print("=" * 70)

result = probe(f"{BASE_URL}{EXCHANGE_INFO_ENDPOINT}")

if result["status"] != 200:
    print(f"❌ Failed: {result.get('body', result.get('error'))[:500]}")
    exit(1)

data = json.loads(result["body"])

# --- بررسی contracts ---
print("\n" + "=" * 70)
print("CONTRACTS ARRAY ANALYSIS")
print("=" * 70)

contracts = data.get("contracts", [])
print(f"✅ تعداد contracts: {len(contracts)}\n")

if len(contracts) == 0:
    print("❌ contracts خالی است.")
    exit(1)

# نمایش اولین contract برای درک ساختار
print("🔍 نمونه اولین contract (RAW):")
print(json.dumps(contracts[0], indent=2))
print()

# لیست همه symbol های contracts
print("=" * 70)
print("ALL CONTRACT SYMBOLS")
print("=" * 70)
contract_symbols = []
for c in contracts:
    if isinstance(c, dict):
        # ممکنه symbol یا symbolName یا contractName باشه
        for key in ["symbol", "symbolName", "contractName", "name", "contract"]:
            if key in c:
                contract_symbols.append((key, c[key]))
                break

# نمایش ۳۰ تای اول
for i, (k, v) in enumerate(contract_symbols[:30]):
    print(f"  [{i:3d}] {k} = {v}")

if len(contract_symbols) > 30:
    print(f"  ... و {len(contract_symbols) - 30} مورد دیگر")
print()

# جستجوی BTC
print("=" * 70)
print("SEARCHING FOR BTC CONTRACTS")
print("=" * 70)

btc_contracts = []
for c in contracts:
    if not isinstance(c, dict):
        continue
    # بررسی همه فیلدهای ممکن
    for key, val in c.items():
        if isinstance(val, str) and "BTC" in val.upper():
            btc_contracts.append(c)
            break

print(f"✅ {len(btc_contracts)} قرارداد BTC پیدا شد.\n")

if btc_contracts:
    # نمایش کامل اولین قرارداد BTC
    print("🔍 نمایش کامل اولین قرارداد BTC:")
    print(json.dumps(btc_contracts[0], indent=2))
    print()

    # جدول خلاصه همه قراردادهای BTC
    print("=" * 70)
    print("BTC CONTRACTS SUMMARY")
    print("=" * 70)
    for i, c in enumerate(btc_contracts):
        symbol = c.get("symbol") or c.get("symbolName") or c.get("contractName") or "?"
        status = c.get("status", "?")
        print(f"  [{i}] symbol={symbol} | status={status}")

    # --- تحلیل فیلدهای قرارداد اول ---
    print()
    print("=" * 70)
    print("FIELD ANALYSIS — اولین قرارداد BTC")
    print("=" * 70)
    target = btc_contracts[0]
    for key, val in target.items():
        val_repr = json.dumps(val)[:100] if isinstance(val, (list, dict)) else str(val)
        print(f"  {key}: {val_repr}")

else:
    print("⚠️ هیچ قرارداد BTC پیدا نشد. لیست کامل contract ها:")
    for c in contracts[:20]:
        print(json.dumps(c, indent=2)[:500])
        print()

print()
print("=" * 70)
print("✅ PROBE COMPLETED")
print("=" * 70)