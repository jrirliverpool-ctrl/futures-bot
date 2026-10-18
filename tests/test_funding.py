"""
test_funding.py — Pre-implementation tests for B2 (Funding).

قرارداد B2:
  - Funding cashflow = -s * N * r
  - s = 1 for LONG, -1 for SHORT
  - N = remaining_size * price * contract_multiplier
  - r = settleRate (can be + or -)
  - Idempotency: state["last_funding_ts"] prevents double-counting.
  - Funding rates are provided via config["funding_rates"] = {ts_ms: rate}

⚠️ این تست‌ها پیش از پیاده‌سازی شکست می‌خورند.
"""

import os
import sys
import copy

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from paper_engine import process_candle, DEFAULT_CONFIG, DEFAULT_STATE


SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}

# زمان‌های تسویه (08:00 UTC)
TS_08 = 1_700_006_400_000  # یک timestamp فرضی
TS_09 = TS_08 + 3_600_000  # یک ساعت بعد


def _candle(ts, price=100.0):
    # ✅ اصلاح: قیمت‌های OHLC برابر شدند تا نوسان قیمت باعث فعال شدن
    # ناخواسته SL/TP/Trailing و تغییر موجودی نشود. تست فقط Funding را می‌سنجد.
    return [ts, str(price), str(price), str(price), str(price), "1000"]


def _make_state(side="LONG", size=10.0, entry=100.0, last_funding_ts=None):
    state = copy.deepcopy(DEFAULT_STATE)
    state["equity"] = 1000.0
    state["initial_equity"] = 1000.0
    state["peak_equity"] = 1000.0
    state["last_funding_ts"] = last_funding_ts
    if side:
        state["position"] = {
            "side": side,
            "entry_price": entry,
            "remaining_size": size,
            "size": size,
            "realized_pnl": 0.0,
            "initial_sl": entry - 10,
            "current_sl": entry - 10,
            "initial_tp1": entry + 20,
            "tp1_hit": False,
            "be_moved": False,
            "trailing_active": False,
            "highest_high_since_entry": entry,
            "lowest_low_since_entry": entry,
            "candles_since_entry": 0,
            "atr_at_entry": 5.0,
            "commission_paid_usd": 0.0,
        }
    return state


def _cfg(funding_rates=None):
    cfg = dict(DEFAULT_CONFIG)
    cfg["contract_multiplier_for_pnl"] = SPECS["contract_multiplier"]
    cfg["funding_rates"] = funding_rates or {}
    return cfg


# 1. نرخ مثبت، پوزیشن LONG (باید پرداخت کند)
def test_funding_positive_long():
    state = _make_state("LONG", size=10.0)
    # N = 10 * 100 * 0.001 = 1.0 USDT
    # r = +0.01 (1%)
    # Expected deduction = 1.0 * 0.01 = 0.01
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - (1000.0 - 0.01)) < 1e-9, f"Expected equity 999.99, got {new_state['equity']}"
    f_events = [e for e in events if e["type"] == "FUNDING"]
    assert len(f_events) == 1, f"Expected 1 FUNDING event, got {len(f_events)}"
    assert abs(f_events[0]["cashflow_usd"] - (-0.01)) < 1e-9, f"Expected cashflow -0.01, got {f_events[0]['cashflow_usd']}"


# 2. نرخ مثبت، پوزیشن SHORT (باید دریافت کند)
def test_funding_positive_short():
    state = _make_state("SHORT", size=10.0)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - (1000.0 + 0.01)) < 1e-9, f"Expected equity 1000.01, got {new_state['equity']}"
    f_events = [e for e in events if e["type"] == "FUNDING"]
    assert len(f_events) == 1, f"Expected 1 FUNDING event, got {len(f_events)}"
    assert abs(f_events[0]["cashflow_usd"] - 0.01) < 1e-9, f"Expected cashflow 0.01, got {f_events[0]['cashflow_usd']}"


# 3. نرخ منفی، پوزیشن LONG (باید دریافت کند)
def test_funding_negative_long():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: -0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - (1000.0 + 0.01)) < 1e-9, f"Expected equity 1000.01, got {new_state['equity']}"
    f_events = [e for e in events if e["type"] == "FUNDING"]
    assert abs(f_events[0]["cashflow_usd"] - 0.01) < 1e-9, f"Expected cashflow 0.01, got {f_events[0]['cashflow_usd']}"


# 4. نرخ منفی، پوزیشن SHORT (باید پرداخت کند)
def test_funding_negative_short():
    state = _make_state("SHORT", size=10.0)
    cfg = _cfg({TS_08: -0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - (1000.0 - 0.01)) < 1e-9, f"Expected equity 999.99, got {new_state['equity']}"
    f_events = [e for e in events if e["type"] == "FUNDING"]
    assert abs(f_events[0]["cashflow_usd"] - (-0.01)) < 1e-9, f"Expected cashflow -0.01, got {f_events[0]['cashflow_usd']}"


# 5. نرخ صفر
def test_funding_zero_rate():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: 0.0})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    new_state, _ = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - 1000.0) < 1e-9, f"Expected equity 1000.0, got {new_state['equity']}"


# 6. بدون پوزیشن
def test_funding_no_position():
    state = _make_state(side=None)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - 1000.0) < 1e-9, f"Expected equity 1000.0, got {new_state['equity']}"
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0, "Expected no FUNDING events"


# 7. پوزیشن بسته شده (نباید اعمال شود)
def test_funding_position_closed():
    state = _make_state("LONG", size=10.0)
    # SL را طوری تنظیم می‌کنیم که کندل آن را بزند
    state["position"]["current_sl"] = 95.0
    cfg = _cfg({TS_08: 0.01})
    # ✅ توجه: این تست کندل اختصاصی خودش را دارد و از _candle استفاده نمی‌کند
    # چون هدف این تست شبیه‌سازی یک کندل نزولی است که SL را فعال کند.
    klines = [[TS_08, "100", "101", "90", "95", "1000"]]
    indicators = {"atr": [1.0]}

    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    # پوزیشن باید بسته شده باشد
    assert new_state["position"] is None, "Position should be closed (None)"
    # و هیچ funding اعمال نشده باشد (چون موقعیت قبل از تسویه بسته شده)
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0, "Expected no FUNDING events after closure"


# 8. تغییر ارزش اسمی با تغییر قیمت
def test_funding_dynamic_notional():
    state = _make_state("LONG", size=10.0, entry=100.0)
    # N در لحظه تسویه = 10 * 110 * 0.001 = 1.1 USDT
    # r = +0.01 → پرداخت = 0.011
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08, price=110.0)]
    indicators = {"atr": [1.0]}

    new_state, _ = process_candle(state, klines, indicators, 0, SPECS, cfg)

    assert abs(new_state["equity"] - (1000.0 - 0.011)) < 1e-9, f"Expected equity 999.989, got {new_state['equity']}"


# 9. تکرارناپذیری (اجرای مجدد همان timestamp)
def test_funding_idempotency():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    state, _ = process_candle(state, klines, indicators, 0, SPECS, cfg)
    # equity الان 999.99 است
    assert abs(state["equity"] - 999.99) < 1e-9, f"Step 1: Expected equity 999.99, got {state['equity']}"
    assert state.get("last_funding_ts") == TS_08, f"Expected last_funding_ts to be {TS_08}, got {state.get('last_funding_ts')}"

    # اجرای مجدد همان کندل
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)

    # نباید دوباره کسر شود
    assert abs(new_state["equity"] - 999.99) < 1e-9, f"Step 2: Expected equity 999.99, got {new_state['equity']}"
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0, "Expected no FUNDING events on duplicate call"


# 10. نبود نرخ تاریخی (باید خطا بدهد، نه فرض صفر)
def test_funding_missing_rate_fails_loud():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({})  # دیکشنری خالی، انتظار می‌رود خطا بدهد
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}

    try:
        process_candle(state, klines, indicators, 0, SPECS, cfg)
        assert False, "Expected exception due to missing funding rate"
    except Exception as e:
        # بررسی می‌کنیم که خطای مربوط به Funding باشد
        assert "funding" in str(e).lower() or "rate" in str(e).lower(), f"Expected funding rate error, got {str(e)}"


if __name__ == "__main__":
    tests = [
        test_funding_positive_long,
        test_funding_positive_short,
        test_funding_negative_long,
        test_funding_negative_short,
        test_funding_zero_rate,
        test_funding_no_position,
        test_funding_position_closed,
        test_funding_dynamic_notional,
        test_funding_idempotency,
        test_funding_missing_rate_fails_loud,
    ]
    passed, failed = 0, 0
    print("=" * 60)
    print(f"🧪 Funding Tests — Pre-Implementation ({len(tests)} total)")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"❌ {t.__name__}: {exc}")
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)