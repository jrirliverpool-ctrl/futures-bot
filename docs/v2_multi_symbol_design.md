## 10. Corrections Applied Before Implementation

پس از بازبینی دقیق، این اصلاحات در نسخه پیاده‌سازی اعمال شد:

1. **Top-level fields must match exactly** — extra fields are rejected.
2. **Per-symbol fields must match exactly** — no extra fields allowed.
3. **Symbol whitelist** — `validate_state_v2(allowed_symbols=...)` enforces the whitelist.
4. **Equity rules**:
   - `initial_equity > 0` (strictly)
   - `equity >= 0` (0 means account blown; engine MUST halt)
   - `peak_equity >= initial_equity`
5. **`daily_loss` and `weekly_loss` are required** — no defaults.
6. **Portfolio capacity** counts BOTH open positions AND pending entries:
   - `_portfolio_capacity_used()` = sum over symbols where `position` or `pending_entry` is set
   - Must be `<= max_concurrent_positions`
7. **Per-symbol invariant** — a symbol cannot have both `position` and `pending_entry`.
8. **Unique backup names** — `state.json.bak.{timestamp}.{uuid8}` prevents collisions.
9. **`delete_state_v2` REMOVED from normal API** — only available via `state_manager_v2_admin.py` with `--confirm`.
10. **`strategy_hash` comparison** is opt-in via `check_hash=True` (default False on save, True on strict load).

## 11. Simulation-Only Clarification

این نسخه از V2 **فقط شبیه‌سازی است**. برای معامله واقعی روی Toobit:

- لایه اجرای سفارش (`order_executor_v2.py`) باید جداگانه طراحی شود
- مدیریت rate limit، retry، و idempotency باید اضافه شود
- تأیید پر شدن سفارش و تطبیق موقعیت با صرافی الزامی است
- کلیدهای API و مجوزهای محدود باید در جای امن باشند
- هیچ کد این ریپو نباید کلید واقعی ذخیره کند

این‌ها در V2.0 پیاده‌سازی نمی‌شوند و به فاز جداگانه V2.1 موکول می‌شوند.

## 12. V1 and V2: Independent Ledgers

⚠️ **مهم**: V1 و V2 دو دفتر حسابداری **کاملاً مستقل** هستند:

- V1: `paper_state_dir/state.json` (BTC only, equity ≈ 984.67)
- V2: `multi_symbol_state_dir/state.json` (3+ symbols, equity = 984.67 initial)

**هرگز** این دو دفتر با هم مخلوط نمی‌شوند. اگر در آینده تصمیم گرفتید V2 را به معامله واقعی وصل کنید، باید:

1. V1 را کاملاً متوقف کنید
2. V2 را با موجودی واقعی حساب راه‌اندازی کنید (نه 984.67)
3. مهاجرت صریح انجام دهید

تا آن زمان، V1 و V2 می‌توانند به صورت موازی شبیه‌سازی شوند.