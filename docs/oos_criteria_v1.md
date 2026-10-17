# OOS Criteria — ApexQuant V1

**Version:** v1.0.0-skeleton
**Status:** SKELETON — NOT APPROVED FOR OOS
**Created at (UTC):** 2026-10-18T00:00:00+00:00

⚠️ این سند یک اسکلت است، نه مجوز اجرای OOS.
⚠️ تا رفع BLOCKERهای B1 و B2 و نهایی‌سازی v1.1.0، هیچ OOS اجرا نمی‌شود.
⚠️ آستانه‌های عددی در فصل ۹ (۲۰٪ DD، ۱.۲ PF، ۶۰٪ پایداری) **پیشنهاد موقت** هستند، نه تصمیم قطعی.

---

## 1. Purpose

این سند معیارهای OOS (Out-of-Sample) استراتژی ApexQuant V1 را تعریف
می‌کند. عبور از این معیارها **پیش‌شرط** شروع دوره‌ی Paper است،
نه تضمین سودآوری.

---

## 2. مرجع رسمی معیارها

`backtest.py` (تابع `run_backtest` و `compute_metrics`) تنها **مرجع رسمی**
معیارهای قبولی OOS است.

`performance.py` فقط **گزارش‌ساز** است — تجزیه‌ی جانبی (Long/Short،
روزانه/هفتگی، rolling DD برای نمودار) انجام می‌دهد، ولی هیچ metric
جدیدی که مبنای قبولی/ردی باشد تولید نمی‌کند.

**پیامد:** هر عددی که در فصل ۹ به‌عنوان معیار قبولی معرفی شود، باید
از `result["metrics"]` گرفته شود، نه از `build_performance_report`.

اگر روزی معیار جدیدی لازم شد، ابتدا باید به `backtest.py.compute_metrics`
اضافه شود، سپس در این سند به‌عنوان معیار قبولی ثبت شود.

---

## 3. BLOCKERS — پیش از OOS باید حل شوند

### BLOCKER B1 — Commission در Backtest و Paper مدل نمی‌شود

**شاهد:** `scripts/backtest.py` docstring: