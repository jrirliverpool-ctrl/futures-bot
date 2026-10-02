"""
test_compute_hash.py — Unit tests for compute_hash.py.
"""

import os
import sys
import json

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from strategy_config import get_strategy_config, STRATEGY_CONFIG

from compute_hash import (
    compute_strategy_hash,
    config_to_canonical_json,
    get_strategy_metadata,
    verify_hash,
)


# ============================================================
# Canonical JSON
# ============================================================

def test_canonical_json_no_whitespace():
    s = config_to_canonical_json({"a": 1, "b": 2})
    assert s == '{"a":1,"b":2}'


def test_canonical_json_sorted_keys():
    s = config_to_canonical_json({"z": 1, "a": 2, "m": 3})
    assert s == '{"a":2,"m":3,"z":1}'


def test_canonical_json_order_independent():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert config_to_canonical_json(a) == config_to_canonical_json(b)


def test_canonical_json_rejects_nan():
    try:
        config_to_canonical_json({"x": float("nan")})
        assert False
    except ValueError:
        pass


def test_canonical_json_rejects_inf():
    try:
        config_to_canonical_json({"x": float("inf")})
        assert False
    except ValueError:
        pass


# ============================================================
# Hash format
# ============================================================

def test_hash_format():
    h = compute_strategy_hash()
    assert isinstance(h, str)
    assert h.startswith("sha256:")
    assert len(h) == 71
    hex_part = h[7:]
    assert all(c in "0123456789abcdef" for c in hex_part)


# ============================================================
# Determinism
# ============================================================

def test_hash_deterministic_same_config():
    h1 = compute_strategy_hash()
    h2 = compute_strategy_hash()
    assert h1 == h2


def test_hash_deterministic_dict_order():
    a = {"version": "1.0.0", "x": 1, "y": 2}
    b = {"y": 2, "version": "1.0.0", "x": 1}
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_hash_default_matches_explicit():
    h1 = compute_strategy_hash()
    h2 = compute_strategy_hash(get_strategy_config())
    assert h1 == h2


# ============================================================
# Sensitivity
# ============================================================

def test_hash_changes_on_each_field():
    base_hash = compute_strategy_hash()

    variations = [
        {"version": "1.0.1"},
        {"timeframe": "4h"},
        {"ema_fast_period": 21},
        {"ema_slow_period": 100},
        {"donchian_period": 10},
        {"atr_period": 7},
        {"sl_atr_mult": 2.0},
        {"tp1_atr_mult": 3.0},
        {"be_trigger_at_r": 1.5},
        {"be_sl_offset_atr": 0.2},
        {"trailing_atr_mult": 2.0},
        {"time_exit_candles": 24},
        {"risk_per_trade": 0.02},
        {"max_leverage": 5},
        {"daily_loss_limit": 0.05},
        {"weekly_loss_limit": 0.10},
        {"total_dd_limit": 0.20},
        {"cooldown_candles": 3},
        {"slippage_pct": 0.001},
    ]

    for v in variations:
        cfg = get_strategy_config(v)
        h = compute_strategy_hash(cfg)
        assert h != base_hash, f"hash did not change for override {v}"


# ============================================================
# Metadata
# ============================================================

def test_get_strategy_metadata():
    meta = get_strategy_metadata()
    assert "version" in meta and "hash" in meta
    assert meta["version"] == "1.0.0"
    assert meta["hash"].startswith("sha256:")


def test_metadata_hash_matches_compute():
    assert get_strategy_metadata()["hash"] == compute_strategy_hash()


# ============================================================
# Verify
# ============================================================

def test_verify_hash_ok():
    cfg = get_strategy_config()
    h = compute_strategy_hash(cfg)
    assert verify_hash(cfg, h) is True


def test_verify_hash_mismatch():
    cfg = get_strategy_config()
    wrong = "sha256:" + "0" * 64
    assert verify_hash(cfg, wrong) is False


def test_verify_hash_invalid_type():
    cfg = get_strategy_config()
    assert verify_hash(cfg, None) is False
    assert verify_hash(cfg, 123) is False
    assert verify_hash(cfg, "") is False


# ============================================================
# Integration with strategy_config
# ============================================================

def test_hash_isolated_from_symbol():
    """symbol نباید hash را تغییر دهد چون در config نیست."""
    h1 = compute_strategy_hash()
    # اضافه کردن symbol به یک dict محلی، نباید hash را عوض کند
    # (چون compute_strategy_hash فقط به config می‌نگرد)
    # این تست خودش ماهیتاً تأیید می‌کند که symbol در config نیست.
    from strategy_config import STRATEGY_CONFIG
    assert "symbol" not in STRATEGY_CONFIG
    h2 = compute_strategy_hash()
    assert h1 == h2


# ============================================================
# Print golden hash (informational)
# ============================================================

def test_print_current_hash():
    h = compute_strategy_hash()
    print(f"\n📌 CURRENT STRATEGY HASH: {h}\n")
    # This test always passes. Value is extracted from CI log.


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_canonical_json_no_whitespace,
        test_canonical_json_sorted_keys,
        test_canonical_json_order_independent,
        test_canonical_json_rejects_nan,
        test_canonical_json_rejects_inf,
        test_hash_format,
        test_hash_deterministic_same_config,
        test_hash_deterministic_dict_order,
        test_hash_default_matches_explicit,
        test_hash_changes_on_each_field,
        test_get_strategy_metadata,
        test_metadata_hash_matches_compute,
        test_verify_hash_ok,
        test_verify_hash_mismatch,
        test_verify_hash_invalid_type,
        test_hash_isolated_from_symbol,
        test_print_current_hash,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Compute Hash Unit Tests")
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