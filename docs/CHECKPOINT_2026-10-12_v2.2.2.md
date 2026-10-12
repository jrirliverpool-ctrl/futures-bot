# Checkpoint — v2.2.2 Data Pipeline Verified

**تاریخ:** 2026-10-12
**شاخه:** main
**نسخه‌ی Probe:** v2.2.2
**وضعیت CI:** ✅ همه‌ی jobها سبز
**آخرین commit در زمان ثبت:** f003258

---

## هدف این checkpoint

ثبت وضعیت تأییدشده‌ی لایه‌ی داده، پیش از شروع کار روی `data_loader.py` و
اتصال به Backtest. این سند صرفاً یک snapshot است؛ هیچ منطق جدیدی اضافه
نمی‌کند و هیچ فایل موجودی را تغییر نمی‌دهد.

---

## آنچه تأیید شده است

### تست‌های خودکار (۱۰۸ تست)

| فایل | تعداد | وضعیت |
|---|---|---|
| `tests/test_binance_feed.py` | ۳۸ | ✅ |
| `tests/test_probe_history.py` | ۴۷ | ✅ |
| `tests/test_probe_known_gaps.py` | ۲۳ | ✅ |
| **جمع** | **۱۰۸** | **✅** |

### Probeها در CI

| مرحله | نتیجه |
|---|---|
| Probe live (اتصال زنده) | ✅ |
| Probe history — short (~۶ ماه) | ✅ |
| Probe history — long (۲۰۱۹→اکنون) | ✅ |
| Verify gaps (مستقل) | ✅ ۲۰/۲۰ confirmed_gap |

### Known gaps

- فایل مرجع: `data/known_gaps_btcusdt_1h.json`
- schema_version: `1.0.0`
- تعداد بازه‌ها: **۲۰**
- تعداد کندل مفقود: **۶۰**
- تأیید مستقل: ۱۰۰ درخواست API، همه‌ی ۲۰ بازه `confirmed_gap`
- برچسب cause فعلی: **همه `unknown_legacy_gap`**
- ۴ بازه با `candidate_cause: binance_scheduled_maintenance` (بدون ارتقا به cause قطعی)

---

## نکات مهم درباره‌ی تفسیر نتایج

### ۱. درصد پوشش، per-run است نه مطلق

آخرین اجرای Probe long نشان داد:

- `expected_count` = ۶۸٬۲۰۱
- `actual_unique` = ۶۸٬۱۴۱
- `missing` = ۶۰ (۰.۰۸۸٪)

درصد پوشش = `68,141 / 68,201 ≈ 99.912%`

**اما این عدد مطلق نیست.** چون `end` به `now()` وصل است، هر بار
اجرا بازه‌ی کمی متفاوت می‌شود. عدد ۹۹.۹۱۲٪ برای آن اجرا در آن لحظه
معتبر است، نه یک ادعای ثابت درباره‌ی منبع داده.

### ۲. `complete_with_known_gaps` معادل `complete` نیست

این دو وضعیت در گزارش `completeness` به‌صورت صریح تفکیک شده‌اند:

- `complete` — هیچ gap در بازه‌ی بررسی‌شده نبود
- `complete_with_known_gaps` — همه‌ی gapها در فهرست known تطبیق یافتند
- `incomplete` — حداقل یک gap ناشناخته یا خطای دیگر

`verdict_ok = True` برای هر دو حالت اول؛ ولی لایه‌ی Backtest باید
بتواند بین‌شان تفاوت بگذارد.

### ۳. لایه‌ی ایمنی در برابر داده‌ی ناقص

اگر بایننس فردا یک gap جدید بسازد، `missing_unknown > 0` خواهد شد،
`completeness = incomplete`، exit code = 1. این رفتار در CI فعال است.

### ۴. علت gapها اعلام قطعی نشده

هیچ‌کدام از ۲۰ بازه، برچسب `binance_scheduled_maintenance` قطعی ندارند.
`candidate_cause` فقط به‌عنوان «فرضیه‌ی قابل‌بررسی» ثبت شده. ارتقای آن به
`cause` مستلزم بایگانی مدرک رسمی در مخزن است (به `data/evidence/` مراجعه).

---

## آنچه هنوز انجام نشده است

1. **بایگانی مدارک ۴ بازه‌ی مشکوک به maintenance** — تسک موازی، اختیاری.
2. **`data_loader.py`** — لایه‌ی داده برای Backtest. هنوز ساخته نشده.
3. **تست‌های رفتار Backtest هنگام عبور از gap** — هنوز ساخته نشده.
4. **اتصال کنترل‌شده به موتور Backtest** — هنوز انجام نشده.

---

## قراردادهایی که از این پس الزامی‌اند

- ❌ هیچ کندل مصنوعی ساخته نمی‌شود.
- ❌ هیچ پرکردن خودکار gap انجام نمی‌شود.
- ❌ هیچ ورود جدید یا سیگنال روی بازه‌ی فاقد داده صادر نمی‌شود.
- ✅ هر gap ناشناخته → توقف Backtest.
- ✅ هر gap known → فقط با سیاست صریح آزموده‌شده پذیرفته می‌شود.
- ✅ هر تغییر در سیاست داده، نیازمند تست جدید و نسخه‌بندی است.

---

## بازگشت‌پذیری

اگر لازم شد به وضعیت پیش از این checkpoint برگردیم:

- تمام فایل‌های مرجع در مخزن commit شده‌اند.
- `data/known_gaps_btcusdt_1h.json` تنها منبع حقیقت برای فهرست gapها است.
- هیچ وابستگی خارجی یا API key در این پروژه استفاده نمی‌شود.

---

## فایل‌های کلیدی وضعیت فعلی

| فایل | نقش |
|---|---|
| `scripts/binance_feed.py` | لایه‌ی دریافت داده |
| `scripts/probe_binance_history.py` | Probe تاریخچه با پشتیبانی `--known-gaps` |
| `scripts/verify_gaps.py` | تأیید مستقل gapها |
| `data/known_gaps_btcusdt_1h.json` | فهرست مرجع ۲۰ gap |
| `tests/test_binance_feed.py` | ۳۸ تست |
| `tests/test_probe_history.py` | ۴۷ تست |
| `tests/test_probe_known_gaps.py` | ۲۳ تست |
| `.github/workflows/probe-binance-history.yml` | CI سه‌مرحله‌ای |

---

**پایان checkpoint.**