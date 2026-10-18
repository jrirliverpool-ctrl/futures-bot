"""
test_funding.py — Pre-implementation tests for B2 (Funding).
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

TS_08 = 1_700_006_400_000
TS_09 = TS_08 + 3_600_000

def _candle(ts, price=100.0):
    # قیمت باز و بسته برابر است تا PnL صفر شود
    return [ts, str(price), str(price+1), str(price-1), str(price), "1000"]

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


def test_funding_positive_long():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - (1000.0 - 0.01)) < 1e-9, f"Expected equity 999.99, got {new_state['equity']}"
    f_events = [e for e in events if e["type"] == "FUNDING"]
    assert len(f_events) == 1
    assert abs(f_events[0]["cashflow_usd"] - (-0.01)) < 1e-9


def test_funding_positive_short():
    state = _make_state("SHORT", size=10.0)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - (1000.0 + 0.01)) < 1e-9, f"Expected equity 1000.01, got {new_state['equity']}"


def test_funding_negative_long():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: -0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - (1000.0 + 0.01)) < 1e-9, f"Expected equity 1000.01, got {new_state['equity']}"


def test_funding_negative_short():
    state = _make_state("SHORT", size=10.0)
    cfg = _cfg({TS_08: -0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - (1000.0 - 0.01)) < 1e-9, f"Expected equity 999.99, got {new_state['equity']}"


def test_funding_zero_rate():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: 0.0})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    new_state, _ = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - 1000.0) < 1e-9, f"Expected equity 1000.0, got {new_state['equity']}"


def test_funding_no_position():
    state = _make_state(side=None)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - 1000.0) < 1e-9
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0


def test_funding_position_closed():
    state = _make_state("LONG", size=10.0)
    state["position"]["current_sl"] = 95.0
    cfg = _cfg({TS_08: 0.01})
    klines = [[TS_08, "100", "101", "90", "95", "1000"]]
    indicators = {"atr": [1.0]}
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert new_state["position"] is None, "Position should be closed"
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0, "Expected no FUNDING events after closure"


def test_funding_dynamic_notional():
    state = _make_state("LONG", size=10.0, entry=100.0)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08, price=110.0)]
    indicators = {"atr": [1.0]}
    new_state, _ = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - (1000.0 - 0.011)) < 1e-9, f"Expected equity 999.989, got {new_state['equity']}"


def test_funding_idempotency():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({TS_08: 0.01})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    state, _ = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(state["equity"] - 999.99) < 1e-9
    assert state.get("last_funding_ts") == TS_08
    new_state, events = process_candle(state, klines, indicators, 0, SPECS, cfg)
    assert abs(new_state["equity"] - 999.99) < 1e-9
    assert len([e for e in events if e["type"] == "FUNDING"]) == 0


def test_funding_missing_rate_fails_loud():
    state = _make_state("LONG", size=10.0)
    cfg = _cfg({})
    klines = [_candle(TS_08)]
    indicators = {"atr": [1.0]}
    try:
        process_candle(state, klines, indicators, 0, SPECS, cfg)
        assert False, "Expected exception due to missing funding rate"
    except Exception as e:
        assert "funding" in str(e).lower() or "rate" in str(e).lower()


if __name__ == "__main__":
    tests = [
        test_funding_positive_long, test_funding_positive_short,
        test_funding_negative_long, test_funding_negative_short,
        test_funding_zero_rate, test_funding_no_position,
        test_funding_position_closed, test_funding_dynamic_notional,
        test_funding_idempotency, test_funding_missing_rate_fails_loud,
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