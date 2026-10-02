# ApexQuant Strategy V1 — Specification

**Version:** 1.0.0  
**Status:** 🟢 FROZEN  
**Last Updated:** 2026-10-02  
**Hash:** _(این مقدار پس از اولین Commit با اسکریپت `compute_hash.py` محاسبه و درج می‌شود)_

---

## 0. اصل بنیادی

> **هیچ پارامتری به‌خاطر بهبود Backtest تغییر نمی‌کند مگر با تست Out-of-Sample معتبر.**

Overfitting بزرگ‌ترین دشمن این پروژه است. هر تغییر در پارامترها باید:
1. با فرضیه‌ی مشخص انجام شود
2. روی In-Sample تنظیم شود
3. روی Out-of-Sample تأیید شود
4. در `CHANGELOG.md` ثبت شود

---

## 1. اطلاعات کلی

| مورد | مقدار |
|---|---|
| نام استراتژی | ApexQuant V1 — Trend Follow / Donchian Breakout |
| نسخه | 1.0.0 |
| بازار | USDT-M Perpetual Futures |
| صرافی | Toobit |
| حالت اجرا (V1) | Paper Trading |
| حالت Live | ⛔ تا تأیید کامل معیارهای Section 14 |

---

## 2. بازار و نماد

| مورد | مقدار |
|---|---|
| Symbol اصلی | `BTC-SWAP-USDT` |
| نوع بازار | Perpetual Futures |
| Symbol های آینده (V2) | `ETH-SWAP-USDT`, `SOL-SWAP-USDT` |
| Warmup لازم | ۲۲۰ کندل (EMA200 + Donchian20) |
| حداکثر Symbol همزمان (V1) | ۱ |

---

## 3. تایم‌فریم و دیتا

| مورد | مقدار |
|---|---|
| Timeframe اصلی | `1h` |
| منبع کندل | Toobit `/quote/v1/klines` |
| نوع کندل | **فقط Closed Candle** |
| رفتار با کندل در حال شکل‌گیری | ❌ حذف می‌شود |
| چک Look-ahead | `last_candle_close_time < now_utc` |
| تعداد کندل درخواستی | ۳۰۰ |
| Timezone | UTC در همه‌جا |

---

## 4. جهت معامله

| مورد | مقدار |
|---|---|
| Long | ✅ |
| Short | ✅ |
| Position Mode | One-way |
| Hedge Mode | ❌ |

---

## 5. اندیکاتورها (V1)

| اندیکاتور | پارامتر | نقش |
|---|---|---|
| EMA 200 | period=200 | فیلتر روند اصلی |
| EMA 50 | period=50 | تأییدیه روند میان‌مدت |
| Donchian High/Low | period=20 | تشخیص Breakout |
| ATR | period=14 | SL/TP/Trailing |

**حذف‌شده‌های آگاهانه:**
- ❌ RSI — بدون اثبات Edge
- ❌ Volume SMA — برای V2
- ❌ Bollinger Bands — برای V2

---

## 6. شرایط ورود

### 🟢 Long Entry — تمام شرایط باید برقرار باشند:
1. `Close > EMA200`
2. `EMA50 > EMA200`
3. `Close > Donchian_High(20)[prev_candle]`

### 🔴 Short Entry — تمام شرایط باید برقرار باشند:
1. `Close < EMA200`
2. `EMA50 < EMA200`
3. `Close < Donchian_Low(20)[prev_candle]`

### ⚪ NO_TRADE اگر یکی از این‌ها برقرار باشد:
- هیچ‌کدام از شرایط Long/Short کامل نیست
- پوزیشن باز وجود دارد (One position rule)
- Circuit breaker فعال شده (Section 9)
- Warmup کامل نشده (`total_candles < 220`)
- `qty_raw < min_qty` (Section 8)

---

## 7. مدیریت معامله (Trade Management)

### 7.1 Entry

| مورد | مقدار |
|---|---|
| Entry Reference | `Open` کندل N+1 |
| Slippage | 0.05% (configurable) |
| ترتیب | Signal در Close کندل N → Entry در Open کندل N+1 |

> **دلیل:** جلوگیری از Look-ahead Bias و تطبیق دقیق Backtest با Paper.

### 7.2 Stop Loss

| مورد | مقدار |
|---|---|
| فرمول Long | `Entry − (1.5 × ATR_signal_candle)` |
| فرمول Short | `Entry + (1.5 × ATR_signal_candle)` |
| 1R تعریف | `1R = 1.5 × ATR_signal_candle = فاصله Entry تا SL` |

### 7.3 Take Profit

| مورد | مقدار |
|---|---|
| TP1 | `Entry ± (2.0 × ATR_signal_candle)` = **+2R** |
| حجم TP1 | ۵۰٪ از پوزیشن بسته می‌شود |
| TP2 | در V1 تعریف نمی‌شود — باقی‌مانده با Trailing مدیریت می‌شود |

### 7.4 Break-Even (BE)

| مورد | مقدار |
|---|---|
| شرط فعال‌سازی | وقتی قیمت به `+1R` رسید = `+1.5 ATR` از Entry |
| عمل | `SL = Entry + (0.1 × ATR_signal_candle)` |
| وضعیت | Default ON (قابل خاموش کردن برای تست) |

> **ترتیب پردازش:** BE **قبل** از TP1 فعال می‌شود (چون 1R < 2R). اگر قیمت مستقیماً به TP1 برسد، ابتدا BE فعال می‌شود (اگر شرطش برقرار باشد)، سپس TP1.

### 7.5 Trailing Stop

| مورد | مقدار |
|---|---|
| فعال‌سازی | **فقط بعد از TP1** |
| فرمول Long | `max(current_sl, highest_high_since_entry − 1.5 × ATR_current)` |
| فرمول Short | `min(current_sl, lowest_low_since_entry + 1.5 × ATR_current)` |
| ATR مورد استفاده | `ATR_current` = ATR آخرین کندل کاملاً بسته‌شده |
| قانون صلب | **SL هرگز عقب‌تر نمی‌رود** |

### 7.6 Exit Rules

معامله بسته می‌شود در اولین شرط:

| اولویت | رویداد | Exit Reason |
|---|---|---|
| 1 | SL hit | `SL` |
| 2 | TP1 hit (۵۰٪) | `TP1_PARTIAL` |
| 3 | Trailing SL hit (پس از TP1) | `TRAILING_SL` |
| 4 | Exit Signal: `Close < EMA50` برای Long | `EXIT_SIGNAL` |
| 5 | Time Exit: ۴۸ کندل پس از ورود | `TIME_EXIT` |

---

## 8. Position Sizing

### 8.1 فرمول
