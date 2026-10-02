"""
test_strategy_config.py — Unit tests for strategy_config.py.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from strategy_config import (
    STRATEGY_CONFIG,
    CANONICAL_KEYS,
    DEFAULT_SYMBOL,
    INTERVAL_MS_MAP,
    get_strategy_config,
    get_indicator_params,
    get_engine_config,
    get_timeframe_interval_ms,
)


# ============================================================
# Static config
# ============================================================

def test_config_has_version():
    assert STRATEGY_CONFIG["version"] == "1.0.0"


def test_config_timeframe_valid():
    tf = STRATEGY_CONFIG["timeframe"]
    assert tf in INTERVAL_MS_MAP


def test_config_parameters_complete():
    """بررسی وجود تمام کلیدهای حیاتی استراتژی."""
    required = [
        "version", "timeframe",
        "ema_fast_period", "ema_slow_period", "donchian_period", "atr_period",
        "sl_atr_mult", "tp1_atr_mult", "be_trigger_at_r",
        "be_sl_offset_atr", "trailing_atr_mult", "time_exit_candles",
        "risk_per_trade", "max_leverage",
        "daily_loss_limit", "weekly_loss_limit", "total_dd_limit",
        "cooldown_candles", "slippage_pct",
    ]
    for k in required:
        assert k in STRATEGY_CONFIG, f"missing key: {k}"


def test_config_does_not_contain_symbol():
    """symbol نباید در config استراتژی باشد."""
    assert "symbol" not in STRATEGY_CONFIG
    assert "asset" not in STRATEGY_CONFIG


def test_canonical_keys_match_config():
    assert set(CANONICAL_KEYS) == set(STRATEGY_CONFIG.keys())


def test_default_symbol():
    assert DEFAULT_SYMBOL == "BTC-SWAP-USDT"


# ============================================================
# get_strategy_config
# ============================================================

def test_get_config_default():
    cfg = get_strategy_config()
    assert cfg == STRATEGY_CONFIG


def test_get_config_override_valid():
    cfg = get_strategy_config({"ema_fast_period": 21})
    assert cfg["ema_fast_period"] == 21
    assert cfg["ema_slow_period"] == 200  # unchanged


def test_get_config_override_unknown_key():
    try:
        get_strategy_config({"not_a_key": 1})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Unknown config keys" in str(exc)


# ============================================================
# Indicator params
# ============================================================

def test_get_indicator_params():
    p = get_indicator_params()
    assert p["ema_fast_p"] == 50
    assert p["ema_slow_p"] == 200
    assert p["donchian_p"] == 20
    assert p["atr_p"] == 14


# ============================================================
# Engine config
# ============================================================

def test_get_engine_config_keys():
    ec = get_engine_config()
    expected = {
        "risk_per_trade", "sl_atr_mult", "tp1_atr_mult",
        "trailing_atr_mult", "be_sl_offset_atr", "time_exit_candles",
        "cooldown_candles", "daily_loss_limit", "weekly_loss_limit",
        "total_dd_limit", "slippage_pct", "interval_ms",
    }
    assert set(ec.keys()) == expected


def test_get_engine_config_values():
    ec = get_engine_config()
    assert ec["risk_per_trade"] == 0.01
    assert ec["sl_atr_mult"] == 1.5
    assert ec["tp1_atr_mult"] == 2.0
    assert ec["interval_ms"] == 3_600_000


# ============================================================
# Timeframe
# ============================================================

def test_timeframe_interval_ms():
    assert get_timeframe_interval_ms() == 3_600_000  # 1h


def test_interval_ms_map_complete():
    for key in ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]:
        assert key in INTERVAL_MS_MAP
        assert INTERVAL_MS_MAP[key] > 0


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_config_has_version,
        test_config_timeframe_valid,
        test_config_parameters_complete,
        test_config_does_not_contain_symbol,
        test_canonical_keys_match_config,
        test_default_symbol,
        test_get_config_default,
        test_get_config_override_valid,
        test_get_config_override_unknown_key,
        test_get_indicator_params,
        test_get_engine_config_keys,
        test_get_engine_config_values,
        test_timeframe_interval_ms,
        test_interval_ms_map_complete,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Strategy Config Unit Tests")
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
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)