# Step 9a — Paper Trading State Persistence

**Version:** 1.0.0  
**Status:** 🟢 FROZEN  
**Created:** 2026-10-05  
**Depends on:** state_manager.py, compute_hash.py, strategy_config.py (Verified)

---

## 1. هدف

اجرای پیوسته‌ی Paper Trading در GitHub Actions به‌گونه‌ای که
state بین اجراهای متوالی دقیقاً بازیابی شود.

هر اجرا از runner تازه شروع می‌شود. state نباید در runner
باقی بماند؛ باید در یک منبع پایدار باشد.

---

## 2. تصمیم معماری

**برنچ اختصاصی `paper-state`** با ساختار:
