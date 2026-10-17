"""
test_run_paper_wiring.py — Integration test بین run_paper و paper_engine.

v2.4 (B1): _enrich_specs_for_engine حالا get_fee_config() را صدا می‌زند.
تست‌های مربوطه باید patch شوند.
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

MOCK_FEE = {
    "open_taker_fee": 0.0005,
    "close_taker_fee": 0.0005,
    "open_maker_fee": 0.0002,
    "close_maker_fee": 0.0002,
    "source": "test-mock",
}

KILL_FILENAME = "KILL"


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


# Config wire-up

def test_enrich_specs_contains_leverage():
    with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
        cfg = _enrich_specs_for_engine(SPECS)
    assert "leverage" in cfg
    assert cfg["leverage"] == 3


def test_enrich_specs_contains_leverage_cap():
    with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
        cfg = _enrich_specs_for_engine(SPECS)
    assert "leverage_cap" in cfg
    assert cfg["leverage_cap"] == 3


def test_enrich_specs_contains_fee_fields():
    with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
        cfg = _enrich_specs_for_engine(SPECS)
    assert cfg["open_taker_fee"] == 0.0005
    assert cfg["close_taker_fee"] == 0.0005


def test_enrich_specs_fail_loud_on_fee_error():
    """B1: fail-loud — اگر get_fee_config خطا بدهد، exception بالا می‌رود"""
    with patch("run_paper.get_fee_config",
               side_effect=ImportError("fee config broken")):
        try:
            _enrich_specs_for_engine(SPECS)
            assert False, "expected exception on fee config failure"
        except ImportError:
            pass


def test_enrich_specs_contains_all_required_fields():
    with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
        cfg = _enrich_specs_for_engine(SPECS)
    required = [
        "risk_per_trade", "leverage", "leverage_cap",
        "sl_atr_mult", "tp1_atr_mult",
        "trailing_atr_mult", "be_sl_offset_atr", "time_exit_candles",
        "cooldown_candles", "daily_loss_limit", "weekly_loss_limit",
        "total_dd_limit", "slippage_pct", "interval_ms",
        "contract_multiplier_for_pnl",
        "open_taker_fee", "close_taker_fee",
    ]
    missing = [k for k in required if k not in cfg]
    assert not missing, f"missing fields: {missing}"


def test_valid_leverage_opens_position():
    with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
        cfg = _enrich_specs_for_engine(SPECS)
    state = _state()
    state["pending_entry"] = _pending_entry()

    candle = _candle(3600000, 85100, 85300, 85000, 85200)
    new_state, events = execute_pending_entry(state, candle, SPECS, cfg)

    assert new_state["position"] is not None
    assert new_state["pending_entry"] is None
    entry = [e for e in events if e["type"] == "ENTRY"]
    assert len(entry) == 1


def test_invalid_leverage_rejects_and_preserves_account():
    original_lev = STRATEGY_CONFIG["leverage_per_trade"]
    original_cap = STRATEGY_CONFIG["max_leverage"]
    try:
        STRATEGY_CONFIG["leverage_per_trade"] = 5
        STRATEGY_CONFIG["max_leverage"] = 3

        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            cfg = _enrich_specs_for_engine(SPECS)

        assert cfg["leverage"] == 5
        assert cfg["leverage_cap"] == 3

        state = _state()
        state["pending_entry"] = _pending_entry()

        equity_before = state["equity"]

        candle = _candle(3600000, 85100, 85300, 85000, 85200)
        new_state, events = execute_pending_entry(state, candle, SPECS, cfg)

        assert new_state["pending_entry"] is None
        assert new_state["position"] is None
        rejected = [e for e in events if e["type"] == "ENTRY_REJECTED"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "leverage_exceeds_cap"
        assert new_state["equity"] == equity_before
    finally:
        STRATEGY_CONFIG["leverage_per_trade"] = original_lev
        STRATEGY_CONFIG["max_leverage"] = original_cap


# Kill Switch wire-up

def test_build_engine_config_includes_kill_switch_fields():
    d = _tmp_dir()
    try:
        state = _state()
        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
        assert "kill_switch_active" in cfg
        assert "kill_switch_reason" in cfg
        assert isinstance(cfg["kill_switch_active"], bool)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_inactive_when_no_sources():
    d = _tmp_dir()
    try:
        state = _state()
        state["kill_switch"] = False
        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
        assert cfg["kill_switch_active"] is False
        assert cfg["kill_switch_reason"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_active_when_kill_file():
    d = _tmp_dir()
    try:
        _create_kill(d)
        state = _state()
        state["kill_switch"] = False
        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
        assert cfg["kill_switch_active"] is True
        assert cfg["kill_switch_reason"] is not None
        assert "KILL" in cfg["kill_switch_reason"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_active_when_state_flag():
    d = _tmp_dir()
    try:
        state = _state()
        state["kill_switch"] = True
        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            cfg = _build_engine_config_with_kill_switch(
                SPECS, state, d, repo_root=None,
            )
        assert cfg["kill_switch_active"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_fail_closed_on_error():
    d = _tmp_dir()
    try:
        state = _state()
        state["kill_switch"] = False

        def raising_check(*args, **kwargs):
            raise RuntimeError("mock kill_switch error")

        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            with patch("run_paper.is_kill_switch_active",
                       side_effect=raising_check):
                cfg = _build_engine_config_with_kill_switch(
                    SPECS, state, d, repo_root=None,
                )

        assert cfg["kill_switch_active"] is True
        assert "mock kill_switch error" in cfg["kill_switch_reason"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_engine_config_fail_closed_on_invalid_output():
    d = _tmp_dir()
    try:
        state = _state()

        with patch("run_paper.get_fee_config", return_value=MOCK_FEE):
            with patch("run_paper.is_kill_switch_active", return_value=None):
                cfg = _build_engine_config_with_kill_switch(
                    SPECS, state, d, repo_root=None,
                )
                assert cfg["kill_switch_active"] is True

            with patch("run_paper.is_kill_switch_active",
                       return_value=False):
                cfg = _build_engine_config_with_kill_switch(
                    SPECS, state, d, repo_root=None,
                )
                assert cfg["kill_switch_active"] is True

            with patch("run_paper.is_kill_switch_active",
                       return_value=("yes", "reason")):
                cfg = _build_engine_config_with_kill_switch(
                    SPECS, state, d, repo_root=None,
                )
                assert cfg["kill_switch_active"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    tests = [
        test_enrich_specs_contains_leverage,
        test_enrich_specs_contains_leverage_cap,
        test_enrich_specs_contains_fee_fields,
        test_enrich_specs_fail_loud_on_fee_error,
        test_enrich_specs_contains_all_required_fields,
        test_valid_leverage_opens_position,
        test_invalid_leverage_rejects_and_preserves_account,
        test_build_engine_config_includes_kill_switch_fields,
        test_build_engine_config_inactive_when_no_sources,
        test_build_engine_config_active_when_kill_file,
        test_build_engine_config_active_when_state_flag,
        test_build_engine_config_fail_closed_on_error,
        test_build_engine_config_fail_closed_on_invalid_output,
    ]
    passed, failed = 0, 0
    failures = []
    print("=" * 60)
    print(f"🧪 Run Paper Wiring Integration Tests ({len(tests)} total)")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failures.append(t.__name__)
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)