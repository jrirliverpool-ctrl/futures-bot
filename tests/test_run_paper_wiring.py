"""
test_run_paper_wiring.py — Integration test بین run_paper و paper_engine.

پوشش (11 تست):
  - Config wire-up (3)
  - Valid / Invalid leverage (2)
  - Kill Switch wire-up (6)

هدف: اثبات انتقال leverage / leverage_cap / kill_switch
از run_paper به engine_config.
"""

import os
import sys
import shutil
import tempfile
import copy
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from run_paper import (
    _enrich_specs_for_engine,
    _build_engine_config_with_kill_switch,
)
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

KILL_FILENAME = "KILL"


# ============================================================
# Helpers
# ============================================================

def _tmp_dir():
    return tempfile.mkdtemp()


def _create_kill(directory):
    path = os.path.join(directory, KILL_FILENAME)
    with open(path, "w") as f:
        f.write("kill")
    return path


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
# Config wire-up (3)
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
# Leverage (2)
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


def test_invalid_leverage_rejects_and_preserves_account():
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

        equity_before = state["equity"]
        dl_before = state["daily_loss"]
        wl_before = state["weekly_loss"]
        cb_before = state["circuit_breaker_active"]

        candle = _candle(3600000, 85100, 85300, 85000, 85200)
        new_state, events = execute_pending_entry(state, candle, SPECS, cfg)

        assert new_state["pending_entry"] is None
        assert new_state["position"] is None
        rejected = [e for e in events if e["type"] == "ENTRY_REJECTED"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "leverage_exceeds_cap"
        assert new_state["equity"] == equity_before
        assert new_state["daily_loss"] == dl_before
        assert new_state["weekly_loss"] == wl_before
        assert new_state["circuit_breaker_active"] == cb_before
        print("✅ test_invalid_leverage_rejects_and_preserves_account")
    finally:
        STRATEGY_CONFIG["leverage_per_trade"] = original_lev
        STRATEGY_CONFIG["max_leverage"] = original_cap


# ============================================================
# Kill Switch wire-up (6)
# ============================================================

def test_build_engine_config_includes_kill_switch_fields():
    """کلیدهای kill_switch_active و kill_switch_reason باید موجود باشند."""
    d = _tmp_dir()
    try:
        state = _state()
        cfg = _build_engine_config_with_kill_switch(
            SPECS, state, d, repo_root=None,
        )
        assert "kill_switch_active" in cfg
        assert "kill_switch_reason" in cfg
        assert isinstance(cfg["kill_switch_active"], bool)
        print("✅ test_build_engine_config_includes_kill_switch_fields")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_inactive_when_no_sources():
    """بدون KILL و بدون state flag → inactive، reason=None."""
    d = _tmp_dir()
    try:
        state = _state()
        state["kill_switch"] = False
        cfg = _build_engine_config_with_kill_switch(
            SPECS, state, d, repo_root=None,
        )
        assert cfg["kill_switch_active"] is False
        assert cfg["kill_switch_reason"] is None
        print("✅ test_build_engine_config_inactive_when_no_sources")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_active_when_kill_file():
    """وجود فایل KILL → active=True + reason شامل مسیر."""
    d = _tmp_dir()
    try:
        _create_kill(d)
        state = _state()
        state["kill_switch"] = False
        cfg = _build_engine_config_with_kill_switch(
            SPECS, state, d, repo_root=None,
        )
        assert cfg["kill_switch_active"] is True
        assert cfg["kill_switch_reason"] is not None
        assert "KILL" in cfg["kill_switch_reason"]
        print("✅ test_build_engine_config_active_when_kill_file")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_active_when_state_flag():
    """state["kill_switch"]=True → active=True."""
    d = _tmp_dir()
    try:
        state = _state()
        state["kill_switch"] = True
        cfg = _build_engine_config_with_kill_switch(
            SPECS, state, d, repo_root=None,
        )
        assert cfg["kill_switch_active"] is True
        assert cfg["kill_switch_reason"] is not None
        assert "kill_switch" in cfg["kill_switch_reason"].lower()
        print("✅ test_build_engine_config_active_when_state_flag")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_fail_closed_on_error():
    """
    اگر is_kill_switch_active خطا بدهد → active=True (fail-closed).
    """
    d = _tmp_dir()
    try:
        state = _state()
        state["kill_switch"] = False

        def raising_check(*args, **kwargs):
            raise RuntimeError("mock kill_switch error")

        with patch("run_paper.is_kill_switch_active",
                   side_effect=raising_check):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )

        assert cfg["kill_switch_active"] is True
        assert cfg["kill_switch_reason"] is not None
        assert "mock kill_switch error" in cfg["kill_switch_reason"]
        print("✅ test_build_engine_config_fail_closed_on_error")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_fail_closed_on_invalid_output():
    """
    اگر is_kill_switch_active مقدار نامعتبر برگرداند → active=True.
    حالت‌ها:
      - None
      - bool خالص (غیر tuple)
      - tuple با طول ≠ 2
      - tuple معتبر ولی active غیر bool
    """
    d = _tmp_dir()
    try:
        state = _state()

        # حالت 1: None
        with patch("run_paper.is_kill_switch_active", return_value=None):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
            assert cfg["kill_switch_active"] is True
            assert "invalid kill_switch output" in cfg["kill_switch_reason"]

        # حالت 2: غیر tuple
        with patch("run_paper.is_kill_switch_active", return_value=False):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
            assert cfg["kill_switch_active"] is True
            assert "invalid kill_switch output" in cfg["kill_switch_reason"]

        # حالت 3: tuple با طول ≠ 2
        with patch("run_paper.is_kill_switch_active",
                   return_value=(False,)):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
            assert cfg["kill_switch_active"] is True

        # حالت 4: active non-bool در tuple معتبر
        with patch("run_paper.is_kill_switch_active",
                   return_value=("yes", "some reason")):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
            assert cfg["kill_switch_active"] is True
            assert "not bool" in cfg["kill_switch_reason"]

        print("✅ test_build_engine_config_fail_closed_on_invalid_output")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Config wire-up (3)
        test_enrich_specs_contains_leverage,
        test_enrich_specs_contains_leverage_cap,
        test_enrich_specs_contains_all_required_fields,
        # Leverage (2)
        test_valid_leverage_opens_position,
        test_invalid_leverage_rejects_and_preserves_account,
        # Kill Switch wire-up (6)
        test_build_engine_config_includes_kill_switch_fields,
        test_build_engine_config_inactive_when_no_sources,
        test_build_engine_config_active_when_kill_file,
        test_build_engine_config_active_when_state_flag,
        test_build_engine_config_fail_closed_on_error,
        test_build_engine_config_fail_closed_on_invalid_output,
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