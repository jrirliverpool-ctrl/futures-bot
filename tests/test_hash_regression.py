
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from compute_hash import (
    compute_strategy_hash,
    get_strategy_metadata,
)

from strategy_config import STRATEGY_CONFIG


GOLDEN_VERSION = "1.1.0"

GOLDEN_HASH = (
    "sha256:0de5d79fa280590cb4f316200ed31c7e110c6d8dee448ca74650bfef95bb07e7"
)

GOLDEN_CONFIG = {
    "version": "1.1.0",
    "timeframe": "1h",
    "ema_fast_period": 50,
    "ema_slow_period": 200,
    "donchian_period": 20,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 2.0,
    "be_trigger_at_r": 1.0,
    "be_sl_offset_atr": 0.1,
    "trailing_atr_mult": 1.5,
    "time_exit_candles": 48,
    "risk_per_trade": 0.001,
    "max_leverage": 3,
    "leverage_per_trade": 3,
    "daily_loss_limit": 0.03,
    "weekly_loss_limit": 0.07,
    "total_dd_limit": 0.15,
    "cooldown_candles": 2,
    "slippage_pct": 0.0005,
}


def test_golden_config_unchanged():
    assert dict(STRATEGY_CONFIG) == GOLDEN_CONFIG


def test_golden_hash_matches():
    assert compute_strategy_hash() == GOLDEN_HASH


def test_golden_config_hash_matches():
    assert compute_strategy_hash(GOLDEN_CONFIG) == GOLDEN_HASH


def test_golden_version_matches():
    metadata = get_strategy_metadata()
    assert metadata["version"] == GOLDEN_VERSION


def test_full_metadata():
    metadata = get_strategy_metadata()

    assert metadata["version"] == GOLDEN_VERSION
    assert metadata["hash"] == GOLDEN_HASH


def test_hash_is_sensitive_to_risk_change():
    modified = dict(GOLDEN_CONFIG)
    modified["risk_per_trade"] = 0.01

    assert compute_strategy_hash(modified) != GOLDEN_HASH


def test_hash_is_deterministic():
    assert compute_strategy_hash() == compute_strategy_hash()
