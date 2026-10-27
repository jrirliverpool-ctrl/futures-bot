"""tests/test_state_manager_v2.py — Unit tests for V2 state schema and I/O."""

import json
import math
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from state_manager_v2 import (
    V2_SCHEMA_VERSION,
    V2_SCHEMA_NAME,
    DEFAULT_INITIAL_EQUITY,
    DEFAULT_SYMBOLS,
    SYMBOL_STATE_FIELDS,
    REQUIRED_TOP_LEVEL_FIELDS,
    default_state_v2,
    validate_state_v2,
    save_state_v2,
    load_state_v2,
    load_or_init_state_v2,
    state_exists_v2,
    _get_state_path_v2,
    _portfolio_capacity_used,
)
from state_manager_v2_admin import delete_state_v2_admin
from compute_hash import compute_strategy_hash


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def valid_state():
    return default_state_v2()


# ============================================================
# default_state_v2
# ============================================================

def test_default_state_uses_984_67():
    s = default_state_v2()
    assert s["initial_equity"] == 984.67
    assert s["equity"] == 984.67
    assert s["peak_equity"] == 984.67


def test_default_state_has_three_default_symbols():
    s = default_state_v2()
    assert set(s["symbols"].keys()) == set(DEFAULT_SYMBOLS)


def test_default_state_has_exact_top_level_fields():
    s = default_state_v2()
    assert set(s.keys()) == REQUIRED_TOP_LEVEL_FIELDS


def test_default_state_has_all_symbol_fields():
    s = default_state_v2()
    for sym_state in s["symbols"].values():
        assert set(sym_state.keys()) == SYMBOL_STATE_FIELDS


def test_default_state_defaults():
    s = default_state_v2()
    assert s["version"] == V2_SCHEMA_VERSION
    assert s["schema"] == V2_SCHEMA_NAME
    assert s["circuit_breaker_active"] is False
    assert s["max_concurrent_positions"] == 1
    assert s["max_portfolio_risk"] == 0.01


def test_default_state_rejects_invalid_initial_equity():
    for bad in [True, "1000", None, -100, 0, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            default_state_v2(initial_equity=bad)


def test_default_state_rejects_empty_symbols():
    with pytest.raises(ValueError):
        default_state_v2(symbols=[])


def test_default_state_rejects_invalid_max_concurrent():
    for bad in [0, -1, True, 1.5, "1"]:
        with pytest.raises(ValueError):
            default_state_v2(max_concurrent_positions=bad)


def test_default_state_rejects_invalid_max_portfolio_risk():
    for bad in [0, -0.1, 1.5, True, "0.01"]:
        with pytest.raises(ValueError):
            default_state_v2(max_portfolio_risk=bad)


# ============================================================
# validate_state_v2
# ============================================================

def test_validate_default_state_passes(valid_state):
    validate_state_v2(valid_state)


def test_validate_rejects_missing_top_level_field(valid_state):
    del valid_state["equity"]
    with pytest.raises(ValueError, match="Top-level fields mismatch"):
        validate_state_v2(valid_state)


def test_validate_rejects_extra_top_level_field(valid_state):
    valid_state["unknown_field"] = 42
    with pytest.raises(ValueError, match="Top-level fields mismatch"):
        validate_state_v2(valid_state)


def test_validate_rejects_wrong_version(valid_state):
    valid_state["version"] = "1.0.0"
    with pytest.raises(ValueError, match="version"):
        validate_state_v2(valid_state)


def test_validate_rejects_wrong_schema(valid_state):
    valid_state["schema"] = "single_symbol"
    with pytest.raises(ValueError, match="schema"):
        validate_state_v2(valid_state)


def test_validate_rejects_missing_strategy_hash(valid_state):
    valid_state["strategy_hash"] = None
    with pytest.raises(ValueError, match="strategy_hash"):
        validate_state_v2(valid_state)


def test_validate_check_hash_true_rejects_mismatch(valid_state):
    valid_state["strategy_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="mismatch"):
        validate_state_v2(valid_state, check_hash=True)


def test_validate_check_hash_false_accepts_mismatch(valid_state):
    valid_state["strategy_hash"] = "sha256:" + "0" * 64
    validate_state_v2(valid_state, check_hash=False)  # should not raise


def test_validate_rejects_negative_equity(valid_state):
    valid_state["equity"] = -1.0
    with pytest.raises(ValueError, match="equity"):
        validate_state_v2(valid_state)


def test_validate_allows_zero_equity(valid_state):
    """equity=0 means account blown; state is valid but engine must halt."""
    valid_state["equity"] = 0.0
    validate_state_v2(valid_state)  # should not raise


def test_validate_rejects_zero_initial_equity(valid_state):
    valid_state["initial_equity"] = 0.0
    with pytest.raises(ValueError, match="initial_equity"):
        validate_state_v2(valid_state)


def test_validate_rejects_peak_below_initial(valid_state):
    valid_state["peak_equity"] = valid_state["initial_equity"] - 1.0
    with pytest.raises(ValueError, match="peak_equity"):
        validate_state_v2(valid_state)


def test_validate_rejects_nan_equity(valid_state):
    valid_state["equity"] = float("nan")
    with pytest.raises(ValueError):
        validate_state_v2(valid_state)


def test_validate_rejects_inf_peak_equity(valid_state):
    valid_state["peak_equity"] = float("inf")
    with pytest.raises(ValueError):
        validate_state_v2(valid_state)


def test_validate_rejects_invalid_symbol_format(valid_state):
    valid_state["symbols"]["BTC"] = valid_state["symbols"]["BTC-SWAP-USDT"]
    with pytest.raises(ValueError, match="symbol key"):
        validate_state_v2(valid_state)


def test_validate_rejects_symbol_not_in_whitelist(valid_state):
    with pytest.raises(ValueError, match="not in allowed_symbols"):
        validate_state_v2(valid_state, allowed_symbols=["BTC-SWAP-USDT"])


def test_validate_rejects_extra_symbol_field(valid_state):
    valid_state["symbols"]["BTC-SWAP-USDT"]["extra_field"] = 42
    with pytest.raises(ValueError, match="fields mismatch"):
        validate_state_v2(valid_state)


def test_validate_rejects_missing_symbol_field(valid_state):
    del valid_state["symbols"]["BTC-SWAP-USDT"]["position"]
    with pytest.raises(ValueError, match="fields mismatch"):
        validate_state_v2(valid_state)


def test_validate_rejects_same_symbol_with_position_and_pending(valid_state):
    valid_state["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    valid_state["symbols"]["BTC-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    with pytest.raises(ValueError, match="both position and pending_entry"):
        validate_state_v2(valid_state)


def test_validate_rejects_too_many_open_positions(valid_state):
    valid_state["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    valid_state["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    with pytest.raises(ValueError, match="Too many open positions"):
        validate_state_v2(valid_state)


def test_validate_rejects_too_many_pending_entries(valid_state):
    valid_state["symbols"]["BTC-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    valid_state["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    with pytest.raises(ValueError, match="Too many pending"):
        validate_state_v2(valid_state)


def test_validate_rejects_position_plus_pending_exceeding_capacity(valid_state):
    """1 position + 1 pending = 2 capacity used, but max is 1."""
    valid_state["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    valid_state["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    with pytest.raises(ValueError, match="Portfolio capacity"):
        validate_state_v2(valid_state)


def test_validate_rejects_bool_as_timestamp(valid_state):
    valid_state["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] = True
    with pytest.raises(ValueError):
        validate_state_v2(valid_state)


def test_validate_rejects_string_as_cooldown(valid_state):
    valid_state["symbols"]["BTC-SWAP-USDT"]["cooldown_until_index"] = "42"
    with pytest.raises(ValueError):
        validate_state_v2(valid_state)


# ============================================================
# Portfolio capacity helper
# ============================================================

def test_portfolio_capacity_counts_both_position_and_pending(valid_state):
    assert _portfolio_capacity_used(valid_state) == 0

    valid_state["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    assert _portfolio_capacity_used(valid_state) == 1

    valid_state["symbols"]["BTC-SWAP-USDT"]["position"] = None
    valid_state["symbols"]["BTC-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    assert _portfolio_capacity_used(valid_state) == 1

    valid_state["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "SHORT"}
    assert _portfolio_capacity_used(valid_state) == 2


# ============================================================
# Save / Load
# ============================================================

def test_save_and_load_roundtrip(tmp_dir, valid_state):
    save_state_v2(tmp_dir, valid_state)
    loaded = load_state_v2(tmp_dir)
    assert loaded["version"] == valid_state["version"]
    assert loaded["equity"] == valid_state["equity"]
    assert set(loaded["symbols"].keys()) == set(valid_state["symbols"].keys())


def test_save_creates_unique_backup_per_save(tmp_dir, valid_state):
    save_state_v2(tmp_dir, valid_state)
    valid_state["equity"] = 990.0
    save_state_v2(tmp_dir, valid_state)
    valid_state["equity"] = 980.0
    save_state_v2(tmp_dir, valid_state)

    backups = [f for f in os.listdir(tmp_dir) if f.startswith("state.json.bak.")]
    assert len(backups) == 2  # first save has no backup, second and third do
    assert len(set(backups)) == len(backups)  # all unique


def test_save_rejects_invalid_state(tmp_dir, valid_state):
    valid_state["equity"] = -1.0
    with pytest.raises(ValueError):
        save_state_v2(tmp_dir, valid_state)


def test_save_atomic_preserves_previous_on_validation_failure(tmp_dir, valid_state):
    """If save fails, the previous state file must be preserved."""
    save_state_v2(tmp_dir, valid_state)
    path = _get_state_path_v2(tmp_dir)
    original = open(path).read()

    # Try to save invalid state
    valid_state["equity"] = -1.0
    with pytest.raises(ValueError):
        save_state_v2(tmp_dir, valid_state)

    # Original must be unchanged
    assert open(path).read() == original


def test_save_atomic_preserves_previous_on_os_replace_failure(tmp_dir, valid_state, monkeypatch):
    """If os.replace fails, the previous state file must be preserved."""
    import state_manager_v2 as sm

    save_state_v2(tmp_dir, valid_state)
    path = _get_state_path_v2(tmp_dir)
    original = open(path).read()

    # Monkeypatch os.replace to fail
    def failing_replace(src, dst):
        raise OSError("Simulated replace failure")

    monkeypatch.setattr(sm.os, "replace", failing_replace)

    valid_state["equity"] = 900.0
    with pytest.raises(OSError):
        save_state_v2(tmp_dir, valid_state)

    # Original must be unchanged
    assert open(path).read() == original

    # No temp files left behind
    temp_files = [f for f in os.listdir(tmp_dir) if f.startswith(".state_v2_")]
    assert temp_files == []


def test_load_rejects_missing_file(tmp_dir):
    with pytest.raises(FileNotFoundError):
        load_state_v2(tmp_dir)


def test_load_rejects_corrupted_json(tmp_dir):
    path = _get_state_path_v2(tmp_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write("not valid json")
    with pytest.raises(json.JSONDecodeError):
        load_state_v2(tmp_dir)


def test_load_strict_true_rejects_hash_mismatch(tmp_dir, valid_state):
    valid_state["strategy_hash"] = "sha256:" + "0" * 64
    save_state_v2(tmp_dir, valid_state)
    with pytest.raises(ValueError, match="mismatch"):
        load_state_v2(tmp_dir, strict=True)


def test_load_strict_false_accepts_hash_mismatch(tmp_dir, valid_state):
    valid_state["strategy_hash"] = "sha256:" + "0" * 64
    save_state_v2(tmp_dir, valid_state)
    loaded = load_state_v2(tmp_dir, strict=False)
    assert loaded["strategy_hash"] == "sha256:" + "0" * 64


def test_load_accepts_allowed_symbols(tmp_dir, valid_state):
    save_state_v2(tmp_dir, valid_state)
    loaded = load_state_v2(tmp_dir, allowed_symbols=list(DEFAULT_SYMBOLS))
    assert loaded is not None


def test_load_rejects_disallowed_symbols(tmp_dir, valid_state):
    save_state_v2(tmp_dir, valid_state)
    with pytest.raises(ValueError, match="not in allowed_symbols"):
        load_state_v2(tmp_dir, allowed_symbols=["BTC-SWAP-USDT"])


# ============================================================
# load_or_init
# ============================================================

def test_load_or_init_creates_new(tmp_dir):
    state = load_or_init_state_v2(tmp_dir)
    assert state["equity"] == DEFAULT_INITIAL_EQUITY
    assert state_exists_v2(tmp_dir)


def test_load_or_init_loads_existing(tmp_dir):
    initial = default_state_v2()
    initial["equity"] = 500.0
    save_state_v2(tmp_dir, initial)
    loaded = load_or_init_state_v2(tmp_dir)
    assert loaded["equity"] == 500.0


# ============================================================
# Isolation from V1
# ============================================================

def test_v2_does_not_touch_v1_state_dir(tmp_dir):
    v1_dir = os.path.join(tmp_dir, "paper_state_dir")
    v2_dir = os.path.join(tmp_dir, "multi_symbol_state_dir")

    os.makedirs(v1_dir, exist_ok=True)
    v1_state_path = os.path.join(v1_dir, "state.json")
    with open(v1_state_path, "w") as f:
        f.write('{"version": "1.0.0", "equity": 1000.0}')

    v1_mtime = os.path.getmtime(v1_state_path)

    state = default_state_v2()
    save_state_v2(v2_dir, state)
    load_state_v2(v2_dir)

    assert os.path.getmtime(v1_state_path) == v1_mtime


# ============================================================
# Admin deletion (separate module)
# ============================================================

def test_admin_delete_removes_state_and_backups(tmp_dir, valid_state):
    save_state_v2(tmp_dir, valid_state)
    valid_state["equity"] = 900.0
    save_state_v2(tmp_dir, valid_state)  # creates backup

    delete_state_v2_admin(tmp_dir, confirmed=True)

    remaining = [f for f in os.listdir(tmp_dir) if "state" in f.lower()]
    assert remaining == []


def test_admin_delete_does_not_touch_v1(tmp_dir, valid_state):
    v1_dir = os.path.join(tmp_dir, "paper_state_dir")
    v2_dir = os.path.join(tmp_dir, "multi_symbol_state_dir")

    os.makedirs(v1_dir, exist_ok=True)
    with open(os.path.join(v1_dir, "state.json"), "w") as f:
        f.write('{"version": "1.0.0"}')

    save_state_v2(v2_dir, valid_state)
    delete_state_v2_admin(v2_dir, confirmed=True)

    # V1 must still exist
    assert os.path.exists(os.path.join(v1_dir, "state.json"))