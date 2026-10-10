"""
probe_historical.py — Probe برای کشف قرارداد تاریخی Klines Toobit.

هدف: تعیین نام واقعی پارامترهای زمانی و رفتار API.

هیچ کد production تغییر نمی‌کند.
هیچ سفارشی ارسال نمی‌شود.
فقط درخواست‌های عمومی (public) خواندن داده.

Probe:
  1. Baseline: /quote/v1/klines بدون پارامتر زمانی
  2. کاندیدهای پارامتر زمانی:
       startTime/endTime
       start_time/end_time
       start/end
       from/to
       startTimestamp/endTimestamp
       begin/end
  3. برای هر کاندید:
       - status
       - تعداد کندل
       - first/last timestamp
       - واحد timestamp (ms/s)
"""

import time
import requests


BASE_URL = "https://api.toobit.com"
ENDPOINT = "/quote/v1/klines"

SYMBOL = "BTC-SWAP-USDT"
INTERVAL = "1h"

# بازه‌ی تست: 2000 ساعت گذشته
NOW_MS = int(time.time() * 1000)
RANGE_MS = 2000 * 3600 * 1000  # 2000 ساعت
START_MS = NOW_MS - RANGE_MS
END_MS = NOW_MS


# ============================================================
# Helpers
# ============================================================

def _req(params: dict) -> dict:
    url = f"{BASE_URL}{ENDPOINT}"
    try:
        r = requests.get(url, params=params, timeout=15)
        return {
            "status": r.status_code,
            "text_preview": r.text[:200] if r.text else "",
            "json": r.json() if r.headers.get("content-type", "").startswith("application/json") else None,
        }
    except Exception as e:
        return {"status": "ERR", "error": str(e)}


def _summarize(label: str, params: dict, resp: dict) -> None:
    print(f"\n─── {label} ───")
    print(f"  params : {params}")
    print(f"  status : {resp.get('status')}")

    if resp.get("status") != 200:
        print(f"  preview: {resp.get('text_preview', resp.get('error', '?'))}")
        return

    data = resp.get("json")
    if not isinstance(data, list):
        print(f"  response is not a list: {type(data).__name__}")
        if isinstance(data, dict):
            print(f"  keys: {list(data.keys())}")
        return

    print(f"  candle count : {len(data)}")
    if not data:
        return

    first = data[0]
    last = data[-1]

    first_ts = int(first[0]) if len(first) > 0 else None
    last_ts = int(last[0]) if len(last) > 0 else None

    def _unit(ts):
        if ts is None:
            return "?"
        # ms: ~1.7e12 برای 2024
        # s:  ~1.7e9
        if ts > 1e12:
            return "ms"
        if ts > 1e9:
            return "s"
        return "?"

    print(f"  first ts : {first_ts} ({_unit(first_ts)})")
    print(f"  last ts  : {last_ts} ({_unit(last_ts)})")
    if first_ts and last_ts:
        span_h = (last_ts - first_ts) / 3600000
        print(f"  span     : ~{span_h:.1f}h")


# ============================================================
# Probe
# ============================================================

def main():
    print("=" * 70)
    print("🔍 PROBE HISTORICAL KLINES — Toobit")
    print("=" * 70)
    print(f"  symbol  : {SYMBOL}")
    print(f"  interval: {INTERVAL}")
    print(f"  now(ms) : {NOW_MS}")
    print(f"  start   : {START_MS}")
    print(f"  end     : {END_MS}")
    print("=" * 70)

    # ---- 1. Baseline ----
    baseline_params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000}
    baseline = _req(baseline_params)
    _summarize("Baseline (no time params)", baseline_params, baseline)

    # ---- 2. Candidates ----
    candidates = [
        ("startTime/endTime (ms)", {"startTime": START_MS, "endTime": END_MS}),
        ("start_time/end_time (ms)", {"start_time": START_MS, "end_time": END_MS}),
        ("start/end (ms)", {"start": START_MS, "end": END_MS}),
        ("from/to (ms)", {"from": START_MS, "to": END_MS}),
        ("startTimestamp/endTimestamp (ms)", {"startTimestamp": START_MS, "endTimestamp": END_MS}),
        ("begin/end (ms)", {"begin": START_MS, "end": END_MS}),
        ("startTime/endTime (s)", {"startTime": START_MS // 1000, "endTime": END_MS // 1000}),
    ]

    for label, extra in candidates:
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000}
        params.update(extra)
        resp = _req(params)
        _summarize(label, params, resp)

    # ---- 3. Behavior with small limit ----
    small_params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 10}
    small = _req(small_params)
    _summarize("Baseline limit=10", small_params, small)

    # ---- 4. Large limit ----
    large_params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1500}
    large = _req(large_params)
    _summarize("Baseline limit=1500 (over cap?)", large_params, large)

    print("\n" + "=" * 70)
    print("✅ PROBE COMPLETED")
    print("=" * 70)


if __name__ == "__main__":
    main()