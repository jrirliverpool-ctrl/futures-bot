"""
test_run_paper_wiring.py — Integration test بین run_paper و paper_engine.

هدف: اثبات انتقال تنظیمات leverage و leverage_cap از
run_paper._enrich_specs_for_engine به execute_pending_entry،
و بررسی نتیجه‌ی مسیر ورود (پذیرش/رد).

این تست wiring را می‌سنجد، نه منطق داخلی calculate_position_size
(که در test_paper_engine.py جداگانه پوشش داده شده).

پوشش: 5 تست.
"""

import os
import sys
import copy

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from run_paper import _enrich_specs_for_engine
from paper_engine import execute_pending_entry, DEFAULT_STATE
from strategy_config import STRATEGY_CONFIG


SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}


def _candle(ts, o, h, l, c):
    return [ts, str(o), str(h), str(l), str(c), "1000"]


def _state():
    return copy.deepcopy(DEFAULT_STATE)


def _pending_entry():
    return {
        "side": "LONG",
        "signal_candle_ts": 1000,
        "atr_at_signal": 500.0,
        "close_at_signal": 85000.0,
    }


# ============================================================
# Config wire-up
# ============================================================

def test_enrich_specs_contains_leverage():
    cfg = _enrich_specs_for_engine(SPECS)
    assert "leverage" in cfg
    assert cfg["leverage"] == 3
    print("✅ test_enrich_specs_contains_leverage")


def test_enrich_specs_contains_leverage_cap():
    cfg = _enrich_specs_for_engine(SPECS)
    assert "leverage_cap" in cfg
    assert cfg["leverage_cap"] == 3
    print("✅ test_enrich_specs_contains_leverage_cap")


def test_enrich_specs_contains_all_required_fields():
    cfg = _enrich_specs_for_engine(SPECS)
    required = [
        "risk_per_trade",
        "leverage",
        "leverage_cap",
        "sl_atr_mult",
        "tp1_atr_mult",
        "trailing_atr_mult",
        "be_sl_offset_atr",
        "time_exit_candles",
        "cooldown_candles",
        "daily_loss_limit",
        "weekly_loss_limit",
        "total_dd_limit",
        "slippage_pct",
        "interval_ms",
        "contract_multiplier_for_pnl",
    ]
    missing = [k for k in required if k not in cfg]
    assert not missing, f"missing fields: {missing}"
    print("✅ test_enrich_specs_contains_all_required_fields")


# ============================================================
# Valid leverage → open
# ============================================================

def test_valid_leverage_opens_position():
    cfg = _enrich_specs_for_engine(SPECS)
    state = _state()
    state["pending_entry"] = _pending_entry()

    candle = _candle(3600000, 85100, 85300, 85000, 85200)
    new_state, events = execute_pending_entry(state, candle, SPECS, cfg)

    assert new_state["position"] is not None
    assert new_state["pending_entry"] is None
    entry = [e for e in events if e["type"] == "ENTRY"]
    assert len(entry) == 1
    print("✅ test_valid_leverage_opens_position")


# ============================================================
# Invalid leverage → reject + account state unchanged
# ============================================================

def test_invalid_leverage_rejects_and_preserves_account():
    """
    leverage بالای cap → رد ورود.

    علاوه بر نبود پوزیشن و پاک شدن pending_entry، بررسی می‌کند
    که equity / daily_loss / weekly_loss / circuit_breaker
    دست‌نخورده بمانند.

    نکته: execute_pending_entry in-place است؛
    snapshot قبل از فراخوانی گرفته می‌شود.
    """
    original_lev = STRATEGY_CONFIG["leverage_per_trade"]
    original_cap = STRATEGY_CONFIG["max_leverage"]
    try:
        STRATEGY_CONFIG["leverage_per_trade"] = 5
        STRATEGY_CONFIG["max_leverage"] = 3

        cfg = _enrich_specs_for_engine(SPECS)
        assert cfg["leverage"] == 5
        assert cfg["leverage_cap"] == 3

        state = _state()
        state["pending_entry"] = _pending_entry()

        # snapshot قبل از اجرا
        equity_before = state["equity"]
        daily_loss_before = state["daily_loss"]
        weekly_loss_before = state["weekly_loss"]
        cb_before = state["circuit_breaker_active"]

        candle = _candle(3600000, 85100, 85300, 85000, 85200)
        new_state, events = execute_pending_entry(state, candle, SPECS, cfg)

        # 1. pending_entry پاک شده
        assert new_state["pending_entry"] is None
        # 2. پوزیشنی باز نشده
        assert new_state["position"] is None
        # 3. ENTRY_REJECTED با دلیل مشخص
        rejected = [e for e in events if e["type"] == "ENTRY_REJECTED"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "leverage_exceeds_cap"
        # 4. وضعیت مالی دست‌نخورده
        assert new_state["equity"] == equity_before
        assert new_state["daily_loss"] == daily_loss_before
        assert new_state["weekly_loss"] == weekly_loss_before
        assert new_state["circuit_breaker_active"] == cb_before

        print("✅ test_invalid_leverage_rejects_and_preserves_account")
    finally:
        STRATEGY_CONFIG["leverage_per_trade"] = original_lev
        STRATEGY_CONFIG["max_leverage"] = original_cap


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_enrich_specs_contains_leverage,
        test_enrich_specs_contains_leverage_cap,
        test_enrich_specs_contains_all_required_fields,
        test_valid_leverage_opens_position,
        test_invalid_leverage_rejects_and_preserves_account,
    ]
    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Run Paper Wiring Integration Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)