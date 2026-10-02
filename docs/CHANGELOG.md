# ApexQuant — Changelog

هر تغییر در پارامترهای استراتژی **باید** در این فایل ثبت شود.
هر ورودی جدید باید شامل **hash** و **تاریخ واقعی Commit** باشد.

فرمت تاریخ: `YYYY-MM-DD` (UTC)

---

## [1.0.0] — 2026-10-02

**Status:** 🟢 FROZEN — Initial Strategy Specification

**Hash:** `sha256:1bb41acdba03cbdb666a71298f059114d37a8903317fab86465e317371277300`

### Strategy Parameters
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
| Max leverage | 3x |
| Daily loss limit | 3% |
| Weekly loss limit | 7% |
| Total DD limit | 15% |

### Execution
| Parameter | Value |
|---|---|
| Slippage | 0.05% |
| Default symbol | BTC-SWAP-USDT (خارج از hash) |

### Notes
- نسخه اولیه استراتژی، مبتنی بر `docs/strategy_v1.md`.
- Regression hash ثبت شد در `tests/test_hash_regression.py`.
- تغییر `strategy_config.py` بدون اجرای فرآیند ۵ مرحله‌ای (Spec + Changelog + Golden Hash) ممنوع.

### Chain of Trust