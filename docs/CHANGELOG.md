
# ApexQuant — Changelog

هر تغییر در پارامترهای استراتژی باید در این فایل ثبت شود.

هر ورودی جدید باید شامل موارد زیر باشد:

- نسخه
- تاریخ واقعی Commit به UTC
- پارامترهای تغییر یافته
- دلیل تغییر
- Strategy Hash
- وضعیت مهاجرت State

---

## [1.1.0] — 2026-10-30

**Status:** MOCK / PAPER CONFIGURATION

**Hash:**

`sha256:0de5d79fa280590cb4f316200ed31c7e110c6d8dee448ca74650bfef95bb07e7`

### Changes from 1.0.1

| Parameter | Old | New |
|---|---:|---:|
| version | 1.0.1 | 1.1.0 |
| risk_per_trade | 0.01 | 0.001 |

### Rationale

کاهش ریسک هر معامله از 1% به 0.1% برای مشاهده و تست امن‌تر سیستم.

این تغییر به معنای کاهش ده‌برابری ریسک اسمی هر معامله است.

### Unchanged Parameters

| Parameter | Value |
|---|---:|
| timeframe | 1h |
| EMA fast | 50 |
| EMA slow | 200 |
| Donchian | 20 |
| ATR | 14 |
| SL ATR multiplier | 1.5 |
| TP1 ATR multiplier | 2.0 |
| BE trigger | 1.0R |
| BE offset | 0.1 ATR |
| Trailing | 1.5 ATR |
| Time exit | 48 candles |
| Max leverage | 3 |
| Leverage per trade | 3 |
| Daily loss limit | 3% |
| Weekly loss limit | 7% |
| Total drawdown limit | 15% |
| Cooldown | 2 candles |
| Slippage | 0.05% |

### Strategy Logic

در این نسخه، تغییر در منطق سیگنال EMA / Donchian / ATR ثبت نشده است.

تغییر اصلی در تنظیم ریسک معامله است.

### State Migration

مهاجرت V1 از 1.0.1 به 1.1.0 فقط با اسکریپت کنترل‌شده زیر مجاز است:

`scripts/migrate_paper_state_v1_0_1_to_v1_1_0.py`

شرایط مهاجرت:

1. تطبیق کامل هش و نسخه مبدأ
2. تطبیق هش و نسخه مقصد
3. نبود پوزیشن باز
4. نبود pending entry
5. بررسی سلامت و تعداد رویدادهای تاریخی
6. ایجاد نسخه پشتیبان از state.json
7. تغییر فقط strategy_hash و strategy_version
8. حفظ کامل equity و سایر اطلاعات
9. حفظ بدون تغییر events.jsonl
10. ثبت گزارش مهاجرت

تاریخچه رویدادهای قدیمی نباید به‌عنوان رویدادهای نسخه 1.1.0 بازنویسی شود.

**وضعیت مهاجرت:** تا زمان اجرای موفق Workflow اختصاصی، تأییدنشده است.

### V2 Safety Notice

State چندنمادی V2 دارای پوزیشن باز است.

این نسخه از State نباید با اسکریپت مهاجرت V1 تغییر داده شود.

مهاجرت V2 نیازمند بررسی مستقل پوزیشن باز، تطبیق و Recovery است.

### Live Authorization

نسخه 1.1.0 به‌هیچ‌وجه به معنای تأیید معاملات واقعی نیست.

LIVE و TOOBIT_DEMO تا تکمیل ممیزی مستقل و تأیید معیارهای ایمنی قفل می‌مانند.

---

## [1.0.1] — 2026-10-08

**Status:** CONFIG FROZEN — V1 clarification

**Hash:**

`sha256:7d608627418f8c9a9f7d157d03d0fdc5e25add19cc35f0cc3ee1cc024c347d90`

### Changes from 1.0.0

| Parameter | Value | Note |
|---|---|---|
| version | 1.0.0 → 1.0.1 | version bump |
| leverage_per_trade | added = 3 | separate from leverage cap |

### Rationale

`max_leverage` و `leverage_per_trade` از هم تفکیک شدند.

در V1 هر دو برابر 3 هستند.

### Strategy Parameters

| Parameter | Value |
|---|---:|
| Timeframe | 1h |
| EMA fast | 50 |
| EMA slow | 200 |
| Donchian | 20 |
| ATR | 14 |
| SL | 1.5 ATR |
| TP1 | 2 ATR |
| BE trigger | +1R |
| BE offset | 0.1 ATR |
| Trailing | 1.5 ATR |
| Time exit | 48 candles |
| Cooldown | 2 candles |

### Risk Management

| Parameter | Value |
|---|---:|
| Risk per trade | 1% |
| Max leverage | 3 |
| Leverage per trade | 3 |
| Daily loss limit | 3% |
| Weekly loss limit | 7% |
| Total DD limit | 15% |

### Execution

| Parameter | Value |
|---|---:|
| Slippage | 0.05% |
| Default symbol | BTC-SWAP-USDT |

### Notes

- Regression hash در `tests/test_hash_regression.py` ثبت شده است.
- منطق سیگنال تغییر نکرده است.
- تغییرات مربوط به اهرم و پیکربندی اجرا بوده‌اند.
