# ApexQuant — Changelog

هر تغییر در پارامترهای استراتژی **باید** در این فایل ثبت شود.
هر ورودی جدید باید شامل **hash** و **تاریخ واقعی Commit** باشد.

فرمت تاریخ: `YYYY-MM-DD` (UTC)

---

## [1.0.1] — 2026-10-08

**Status:** 🟢 CONFIG FROZEN — V1 clarification

**Hash:** `sha256:7d608627418f8c9a9f7d157d03d0fdc5e25add19cc35f0cc3ee1cc024c347d90`

### Changes from 1.0.0
| Parameter | Value | Note |
|---|---|---|
| `version` | `1.0.0` → `1.0.1` | version bump |
| `leverage_per_trade` | **added** = 3 | per-trade leverage, جدا از cap |

### Rationale
`max_leverage` (سقف سخت) و `leverage_per_trade` (اهرم واقعی هر معامله)
از هم تفکیک شدند. در V1 هر دو ۳ هستند، ولی تفکیک امکان کاهش اهرم
را بدون تغییر cap فراهم می‌کند.

### Configuration Impact
- `get_engine_config()` اکنون `leverage` و `leverage_cap` را برمی‌گرداند
- `calculate_position_size()` در `paper_engine.py` این دو پارامتر
  را به‌عنوان ورودی اختیاری می‌پذیرد و در صورت ارسال، آن‌ها را
  اعتبارسنجی می‌کند (fail-closed).
- مسیر end-to-end در `run_paper.py` نیازمند اتصال صریح است
  (Step بعدی).

### Strategy Parameters (unchanged)
| Parameter | Value |
|---|---|
| Timeframe | 1h |
| EMA fast | 50 |
| EMA slow | 200 |
| Donchian | 20 |
| ATR | 14 |
| SL | 1.5 × ATR |
| TP1 | 2.0 × ATR (50% partial) |
| BE trigger | +1R |
| BE SL offset | 0.1 × ATR |
| Trailing | 1.5 × ATR (after TP1) |
| Time exit | 48 candles |
| Cooldown | 2 candles |

### Risk Management
| Parameter | Value |
|---|---|
| Risk per trade | 1% |
| Max leverage (cap) | 3 |
| Leverage per trade | 3 |
| Daily loss limit | 3% |
| Weekly loss limit | 7% |
| Total DD limit | 15% |

### Execution
| Parameter | Value |
|---|---|
| Slippage | 0.05% |
| Default symbol | BTC-SWAP-USDT |

### Notes
- Regression hash v1.0.1 در `tests/test_hash_regression.py` ثبت شد.
- هیچ تغییری در منطق استراتژی (SL/TP/Trailing/Time Exit) رخ نداد.
- اتصال `run_paper.py` به `get_engine_config()` در گام بعدی انجام می‌شود.

### Chain of Trust