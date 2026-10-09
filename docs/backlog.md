# ApexQuant — Backlog

موارد باز که تصمیم نهایی برایشان گرفته نشده است.

---

## [BACKLOG-001] Strategy Hash Mismatch

**Status:** 🔵 OPEN
**Priority:** 🟡 Medium — becomes High before Reconciler Wiring
**Created:** 2026-10-10

### خلاصه

state ذخیره‌شده در برنچ `paper-state` hash قدیمی
`1bb41acd...` (V1.0.0) را دارد، ولی کد فعلی hash
`7d608627...` (V1.0.1) را تولید می‌کند.

| فایل | Hash |
|---|---|
| `state.json` (paper-state) | `sha256:1bb41acd...` (V1.0.0) |
| `compute_strategy_hash()` فعلی | `sha256:7d608627...` (V1.0.1) |

### علت ریشه‌ای

بین ساخت state اولیه و اکنون، تنظیمات استراتژی از V1.0.0
به V1.0.1 ارتقا یافته است. تاریخ دقیق این تغییر در
`docs/CHANGELOG.md` ثبت شده است و باید از همان منبع
تأیید شود — نه از این سند.

تغییرات V1.0.1 (طبق CHANGELOG):

1. `STRATEGY_CONFIG["version"]`: `"1.0.0"` → `"1.0.1"`
2. افزودن کلید جدید: `"leverage_per_trade": 3`

هر دوی این تغییرات `canonical_json` را عوض می‌کنند و در
نتیجه hash جدید تولید می‌شود. state موجود که قبل از این
تغییر ساخته شده بود، hash قدیمی را در خود نگه داشت.

### تأثیر فعلی

| بخش | تأثیر |
|---|---|
| `run_paper.py` | ❌ هیچ (hash را چک نمی‌کند) |
| `paper_engine.py` | ❌ هیچ |
| `state_manager.validate_state` | ❌ هیچ (hash در required fields نیست) |
| `reconciler.reconcile_public` | 🔴 **ERROR** اگر به run_paper وصل شود |
| Paper Trading فعلی | ❌ هیچ |

### چه زمانی مهم می‌شود

بعد از اتصال `reconciler` به `run_paper.py`.
در آن مرحله، `hash_match` check فعال می‌شود و state
موجود به‌عنوان ناسازگار علامت‌گذاری می‌شود.

### سه گزینه‌ی محتمل (بدون ترجیح)

#### گزینه ۱: Migration رسمی
- به‌روزرسانی `strategy_hash` و `strategy_version` در state
- یک Commit روی `paper-state` با پیام migration
- نیازمند: یک اسکریپت migration و سند CHANGELOG جداگانه

#### گزینه ۲: Reset State
- حذف `state.json` و `events.jsonl` و ساخت state تازه
- ⚠️ این گزینه **بدون بررسی دقیق** توصیه نمی‌شود:
  - ممکن است اطلاعاتی فراتر از `probe_counter` و heartbeat
    در state یا events وجود داشته باشد.
  - در صورت وجود پوزیشن باز، Reset باعث می‌شود ربات
    نتواند آن را مدیریت کند.
  - پیش از هر Reset باید:
    - نسخه‌ی پشتیبان از `state.json` و `events.jsonl` گرفته شود.
    - بررسی شود که `position` و `pending_entry` هر دو `None` هستند.
    - مقدار `probe_counter` و تعداد event ها ثبت شود.

#### گزینه ۳: Relax Hash Check در Reconciler
- افزودن پارامتر `check_hash=False` به `reconcile_public`
- ⚠️ این گزینه فقط به‌عنوان **احتمال** ثبت می‌شود، نه
  راه‌حل ترجیحی.
- دلیل: محافظت Section 15 در Spec بر مبنای تطابق hash است.
  غیرفعال کردن آن، این محافظت را تضعیف می‌کند.

### تصمیم

⏸ **معوق** — تا قبل از Reconciler Wiring
مستند مرجع: `docs/CHANGELOG.md` (ورودی `[1.0.1]`)

### یادداشت

- منطق استراتژی (EMA/Donchian/ATR/SL/TP/BE/Trailing) عملاً
  تغییر نکرده است؛ فقط ساختار config گسترش یافته.
- این مسئله هیچ ارتباطی با Kill Switch ندارد و در اجراهای
  فعلی Paper Trading هیچ تأثیری ایجاد نمی‌کند.
- تا زمان تصمیم‌گیری، **هیچ تغییری در state.json یا
  events.jsonl اعمال نمی‌شود**.