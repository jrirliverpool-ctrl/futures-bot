import requests
import json
from datetime import datetime, timezone

BASE_URL = "https://api.toobit.com"

EXPECTED_FIELDS = 6
MIN_KLINES = 250
MAX_GAP_MULTIPLIER = 2
INTERVAL_MS = 60 * 60 * 1000

def fetch_klines(symbol: str, interval: str, limit: int = 300):
    # اصلاح مسیر بر اساس مستندات رسمی Toobit و CCXT
    endpoint = "/quote/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}

def validate_klines(klines, symbol: str, interval: str):
    errors = []
    warnings = []

    if not isinstance(klines, list):
        errors.append(f"❌ نوع داده خروجی لیست نیست. دریافت شد: {type(klines).__name__}")
        return errors, warnings

    if len(klines) < MIN_KLINES:
        errors.append(f"❌ تعداد کندل ناکافی: {len(klines)} < {MIN_KLINES}")
    else:
        print(f"✅ تعداد کندل کافی است: {len(klines)}")

    for i, k in enumerate(klines):
        if not isinstance(k, list):
            errors.append(f"❌ کندل شماره {i} یک لیست نیست: {type(k).__name__}")
            break
        if len(k) < EXPECTED_FIELDS:
            errors.append(f"❌ کندل شماره {i} کمتر از {EXPECTED_FIELDS} فیلد دارد: {len(k)}")
            break

    non_numeric = []
    for i, k in enumerate(klines):
        for idx in range(1, EXPECTED_FIELDS):
            try:
                float(k[idx])
            except (ValueError, TypeError):
                non_numeric.append((i, idx, k[idx]))
    if non_numeric:
        errors.append(f"❌ {len(non_numeric)} مقدار غیرعددی در OHLCV پیدا شد. نمونه: {non_numeric[0]}")
    else:
        print("✅ تمام مقادیر OHLCV عددی هستند.")

    timestamps = [int(k[0]) for k in klines if isinstance(k, list) and len(k) >= 1]
    out_of_order = []
    for i in range(1, len(timestamps)):
        if timestamps[i] <= timestamps[i - 1]:
            out_of_order.append((i, timestamps[i - 1], timestamps[i]))
    if out_of_order:
        errors.append(f"❌ {len(out_of_order)} مورد عدم ترتیب زمانی. نمونه: {out_of_order[0]}")
    else:
        print("✅ ترتیب زمانی صعودی است.")

    gaps = []
    for i in range(1, len(timestamps)):
        delta = timestamps[i] - timestamps[i - 1]
        if delta > INTERVAL_MS * MAX_GAP_MULTIPLIER:
            gaps.append({
                "index": i,
                "gap_ms": delta,
                "gap_hours": round(delta / INTERVAL_MS, 2),
                "from": datetime.fromtimestamp(timestamps[i - 1] / 1000, tz=timezone.utc).isoformat(),
                "to": datetime.fromtimestamp(timestamps[i] / 1000, tz=timezone.utc).isoformat(),
            })
    if gaps:
        warnings.append(f"⚠️ {len(gaps)} گپ زمانی بزرگ پیدا شد (نمونه: {gaps[0]['gap_hours']} ساعت)")
    else:
        print("✅ هیچ گپ زمانی غیرعادی وجود ندارد.")

    invalid_ohlc = []
    for i, k in enumerate(klines):
        try:
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            if not (l <= o <= h and l <= c <= h and l <= h):
                invalid_ohlc.append((i, o, h, l, c))
        except (ValueError, TypeError, IndexError):
            continue
    if invalid_ohlc:
        errors.append(f"❌ {len(invalid_ohlc)} کندل با OHLC نامعتبر (High/Low اشتباه). نمونه: {invalid_ohlc[0]}")
    else:
        print("✅ ساختار OHLC تمام کندل‌ها معتبر است.")

    negative_volume = []
    for i, k in enumerate(klines):
        try:
            if float(k[5]) < 0:
                negative_volume.append((i, k[5]))
        except (ValueError, TypeError, IndexError):
            continue
    if negative_volume:
        errors.append(f"❌ {len(negative_volume)} کندل با حجم منفی. نمونه: {negative_volume[0]}")
    else:
        print("✅ حجم تمام کندل‌ها غیرمنفی است.")

    return errors, warnings

print("🔍 دریافت داده‌های بازار فیوچرز Toobit...")
symbol = "BTC-SWAP-USDT"
interval = "1h"
klines = fetch_klines(symbol, interval, 300)

if isinstance(klines, dict) and "error" in klines:
    print(f"❌ خطا در دریافت کندل‌ها: {klines['error']}")
    exit(1)

print(f"\n🧪 شروع اعتبارسنجی داده ({symbol} / {interval})...\n")
errors, warnings = validate_klines(klines, symbol, interval)

print("\n📊 آخرین ۳ کندل:")
for k in klines[-3:]:
    ts = int(k[0]) / 1000
    readable_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"  {readable_time} | O:{k[1]} H:{k[2]} L:{k[3]} C:{k[4]} V:{k[5]}")

print("\n" + "=" * 50)
if warnings:
    for w in warnings:
        print(w)
if errors:
    for e in errors:
        print(e)
    print("\n🏁 FINAL RESULT: FAILED ❌")
    exit(1)
else:
    print("🏁 FINAL RESULT: ALL VALIDATIONS PASSED ✅")