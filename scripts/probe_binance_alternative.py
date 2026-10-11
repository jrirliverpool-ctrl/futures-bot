"""
probe_binance_alternative.py — جستجوی endpoint عمومی Binance بدون geo-block.

هدف: کشف endpoint جایگزین برای دریافت داده‌ی تاریخی Binance.

کاندیدها:
  1. data-api.binance.vision/api/v3/klines
  2. api1.binance.com/api/v3/klines
  3. api2.binance.com/api/v3/klines
  4. api3.binance.com/api/v3/klines
  5. api4.binance.com/api/v3/klines
  6. api-gcp.binance.com/api/v3/klines
  7. api.binance.us/api/v3/klines  (US endpoint — احتمالاً هم block)

Probe فقط درخواست کوچک می‌زند و ساختار پاسخ را چک می‌کند.
"""

import requests


SYMBOL = "BTCUSDT"
INTERVAL = "1h"


CANDIDATES = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines",
    "https://api4.binance.com/api/v3/klines",
    "https://api-gcp.binance.com/api/v3/klines",
    "https://api.binance.us/api/v3/klines",
]


# ============================================================
# Helpers
# ============================================================

def _probe(url: str) -> dict:
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 10}
    try:
        r = requests.get(url, params=params, timeout=10)
        result = {
            "status": r.status_code,
            "preview": r.text[:150] if r.text else "",
            "is_candle_list": False,
            "count": 0,
        }
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    sample = data[0]
                    if isinstance(sample, list) and len(sample) >= 6:
                        result["is_candle_list"] = True
                        result["count"] = len(data)
            except ValueError:
                result["preview"] = "invalid JSON: " + result["preview"]
        return result
    except Exception as e:
        return {
            "status": "ERR",
            "preview": f"{type(e).__name__}: {e}",
            "is_candle_list": False,
            "count": 0,
        }


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 72)
    print("🔍 PROBE BINANCE ALTERNATIVE ENDPOINTS")
    print("=" * 72)
    print(f"  symbol   : {SYMBOL}")
    print(f"  interval : {INTERVAL}")
    print(f"  candidates: {len(CANDIDATES)}")
    print("=" * 72)

    working = []

    for url in CANDIDATES:
        print(f"\n─── {url} ───")
        result = _probe(url)
        print(f"  status       : {result['status']}")
        print(f"  candle list  : {result['is_candle_list']}")
        if result["is_candle_list"]:
            print(f"  count        : {result['count']}")
        else:
            print(f"  preview      : {result['preview'][:100]}")

        if result["is_candle_list"]:
            working.append(url)

    print()
    print("=" * 72)
    print("📊 SUMMARY")
    print("=" * 72)
    if working:
        print(f"✅ {len(working)} working endpoint(s):")
        for w in working:
            print(f"   • {w}")
    else:
        print("❌ هیچ endpoint جایگزینی جواب نداد.")
        print("→ باید سراغ صرافی دیگری برویم (Bybit، OKX، ...)")
    print("=" * 72)


if __name__ == "__main__":
    main()