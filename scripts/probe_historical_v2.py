"""
probe_historical_v2.py — Probe دوم برای endpointهای historical Toobit.

هدف: بررسی وجود endpoint جداگانه برای داده تاریخی چندساله.

سه‌اصلاح نسبت به نسخه‌ی اول:

  ۱. HTTP 200 به‌تنهایی موفقیت نیست:
     - بدنه باید JSON معتبر باشد
     - ساختار باید شامل لیست کندل باشد
     - کندل‌ها باید timestamp معتبر داشته باشند

  ۲. اگر پاسخ «نماد معتبر نیست» بود، endpoint قطعی رد نمی‌شود
     (ممکن است قالب نماد متفاوت باشد).

  ۳. تاریخچه‌ی قدیمی باید اثبات شود:
     - مقایسه‌ی timestamp اولین کندل در دو درخواست
       (با و بدون startTime)
     - اگر بازه تغییر نکرد، فیلتر زمانی مؤثر نیست

اگر هیچ endpoint جواب نداد → تصمیم: مهاجرت به Binance Public.
"""

import json
import time
import requests


BASE_URL = "https://api.toobit.com"
SYMBOL = "BTC-SWAP-USDT"
INTERVAL = "1h"

NOW_MS = int(time.time() * 1000)
START_MS = NOW_MS - 2000 * 3600 * 1000  # 2000 ساعت پیش (~83 روز)


# ============================================================
# Candidates
# ============================================================

CANDIDATES = [
    "/api/v1/market/historical-klines",
    "/api/v1/market/history-candles",
    "/api/v1/futures/history-klines",
    "/api/v1/futures/market/history-klines",
    "/api/v1/futures/market/historical-candles",
    "/api/v1/quote/v1/history-candles",
    "/api/v1/futures/kline/history",
    "/api/v1/futures/historical-klines",
]


# ============================================================
# Helpers
# ============================================================

def _looks_like_candle_list(data) -> tuple:
    """
    بررسی اینکه داده یک لیست کندل معتبر است.
    Returns: (is_valid: bool, detail: str)
    """
    if not isinstance(data, list):
        return False, f"not a list (type={type(data).__name__})"
    if len(data) == 0:
        return False, "empty list"

    sample = data[0]
    if not isinstance(sample, list):
        return False, f"candle is not a list (type={type(sample).__name__})"
    if len(sample) < 6:
        return False, f"candle has only {len(sample)} fields"

    try:
        ts = int(sample[0])
    except (ValueError, TypeError):
        return False, f"first field is not int: {sample[0]!r}"

    # واحد زمان: ms یا s
    if ts > 1e12:
        unit = "ms"
    elif ts > 1e9:
        unit = "s"
    else:
        return False, f"unrecognized timestamp magnitude: {ts}"

    return True, f"valid ({len(data)} candles, unit={unit})"


def _probe(url: str, params: dict) -> dict:
    """یک درخواست می‌زند و نتیجه‌ی پارس‌شده را برمی‌گرداند."""
    result = {
        "status": None,
        "valid_json": False,
        "is_candle_list": False,
        "candle_count": 0,
        "first_ts": None,
        "last_ts": None,
        "unit": None,
        "error": None,
        "raw_preview": "",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        result["status"] = r.status_code
        result["raw_preview"] = r.text[:200] if r.text else ""

        if r.status_code != 200:
            return result

        try:
            data = r.json()
        except ValueError:
            result["error"] = "invalid JSON"
            return result

        result["valid_json"] = True

        # حالت ممکن: {"data": [...]} یا {"result": [...]} یا مستقیم [...]
        candidate_list = None
        if isinstance(data, list):
            candidate_list = data
        elif isinstance(data, dict):
            for key in ("data", "result", "list", "klines"):
                if key in data and isinstance(data[key], list):
                    candidate_list = data[key]
                    break

        if candidate_list is None:
            result["error"] = (
                f"no candle list found in response (keys="
                f"{list(data.keys()) if isinstance(data, dict) else 'N/A'})"
            )
            return result

        valid, detail = _looks_like_candle_list(candidate_list)
        if not valid:
            result["error"] = detail
            return result

        result["is_candle_list"] = True
        result["candle_count"] = len(candidate_list)
        result["first_ts"] = int(candidate_list[0][0])
        result["last_ts"] = int(candidate_list[-1][0])
        result["unit"] = "ms" if result["first_ts"] > 1e12 else "s"

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def _report(endpoint: str, params: dict, result: dict) -> bool:
    """
    چاپ گزارش. Returns: True اگر پاسخ واقعاً معنی‌دار بود.
    """
    status = result.get("status")
    marker = "✅" if result.get("is_candle_list") else "  "
    print(f"{marker} {endpoint}")
    print(f"    params : {params}")
    print(f"    status : {status}")

    if result.get("is_candle_list"):
        print(f"    candles: {result['candle_count']}")
        print(f"    first  : {result['first_ts']} ({result['unit']})")
        print(f"    last   : {result['last_ts']} ({result['unit']})")
        return True
    else:
        err = result.get("error") or "unknown"
        print(f"    reason : {err}")
        if result.get("raw_preview"):
            print(f"    preview: {result['raw_preview'][:120]}")
        return False


def _is_history_effective(endpoint: str) -> tuple:
    """
    بررسی می‌کند آیا startTime واقعاً بازه را به عقب می‌برد.

    Returns: (effective: bool, detail: str)
    """
    # درخواست ۱: بدون time params
    r1 = _probe(f"{BASE_URL}{endpoint}",
                {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000})
    if not r1.get("is_candle_list"):
        return False, f"baseline failed: {r1.get('error')}"

    # درخواست ۲: با startTime
    r2 = _probe(f"{BASE_URL}{endpoint}",
                {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000,
                 "startTime": START_MS, "endTime": NOW_MS})
    if not r2.get("is_candle_list"):
        return False, f"with-time-params failed: {r2.get('error')}"

    # مقایسه‌ی first_ts
    if r2["first_ts"] < r1["first_ts"] - 3600 * 1000:
        delta_h = (r1["first_ts"] - r2["first_ts"]) / 3600000
        return True, (
            f"history extended by ~{delta_h:.1f}h "
            f"(baseline first={r1['first_ts']}, "
            f"with-params first={r2['first_ts']})"
        )
    else:
        return False, (
            f"startTime NOT effective "
            f"(baseline first={r1['first_ts']}, "
            f"with-params first={r2['first_ts']})"
        )


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 72)
    print("🔍 PROBE HISTORICAL V2 — Toobit (targeted, 8 candidates)")
    print("=" * 72)
    print(f"  symbol   : {SYMBOL}")
    print(f"  interval : {INTERVAL}")
    print(f"  range ms : {START_MS} → {NOW_MS}")
    print(f"  candidates: {len(CANDIDATES)} endpoints")
    print("=" * 72)

    base_params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000}
    time_params = dict(base_params)
    time_params.update({"startTime": START_MS, "endTime": NOW_MS})

    valid_endpoints = []

    # ---- Pass 1: کشف endpointهایی که کندل معتبر برمی‌گردانند ----
    print("\n" + "─" * 72)
    print("Pass 1 — endpoint discovery (valid candle list required)")
    print("─" * 72)

    for endpoint in CANDIDATES:
        url = f"{BASE_URL}{endpoint}"

        r_base = _probe(url, base_params)
        base_ok = _report(f"{endpoint} [no time]", base_params, r_base)

        r_time = _probe(url, time_params)
        time_ok = _report(f"{endpoint} [with time]", time_params, r_time)

        if base_ok or time_ok:
            valid_endpoints.append(endpoint)
        print()

    # ---- Pass 2: بررسی اثربخشی فیلتر زمانی ----
    print("─" * 72)
    print("Pass 2 — تاریخچه‌ی قدیمی: آیا startTime مؤثر است؟")
    print("─" * 72)

    if not valid_endpoints:
        print("❌ هیچ endpoint معتبری پیدا نشد.")
        print("\n→ تصمیم: مهاجرت به Binance Public برای داده تاریخی.")
        return

    any_effective = False
    for endpoint in valid_endpoints:
        effective, detail = _is_history_effective(endpoint)
        marker = "✅" if effective else "⚠️ "
        print(f"{marker} {endpoint}")
        print(f"    {detail}")
        if effective:
            any_effective = True

    # ---- نتیجه‌گیری ----
    print()
    print("=" * 72)
    print("📊 FINAL DECISION")
    print("=" * 72)

    if any_effective:
        print("✅ حداقل یک endpoint با فیلتر زمانی مؤثر پیدا شد.")
        print("→ ادامه با Toobit historical data.")
    else:
        print("⚠️ endpoints معتبر بودند ولی فیلتر زمانی مؤثر نبود.")
        print("→ رفتار: مهاجرت به Binance Public (نتیجه‌ی V2 قابل استفاده نیست).")

    print("=" * 72)


if __name__ == "__main__":
    main()