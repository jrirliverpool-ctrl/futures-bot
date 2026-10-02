"""
test_state_manager.py — Unit tests for state_manager.py.
"""

import os
import sys
import json
import tempfile

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from state_manager import (
    STATE_SCHEMA_VERSION,
    TRADE_LOG_COLUMNS,
    default_state,
    validate_state,
    save_state,
    load_state,
    load_or_init_state,
    state_exists,
    delete_state,
    append_trade_events,
    load_trade_log,
    trade_log_exists,
    delete_trade_log,
)

from strategy_config import STRATEGY_CONFIG
from compute_hash import compute_strategy_hash


# ============================================================
# Helpers
# ============================================================

def _tmp_path(suffix=""):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.remove(path)
    return path


def _sample_entry():
    return {
        "type": "ENTRY",
        "side": "LONG",
        "price": 85000.0,
        "size": 0.001,
        "sl": 84250.0,
        "tp1": 86000.0,
        "timestamp": 1790960400000,
    }


def _sample_exit():
    return {
        "type": "EXIT",
        "reason": "SL",
        "price": 84250.0,
        "pnl_usd": -10.0,
        "exit_price_adj": 84200.0,
        "ambiguous": False,
        "remaining_size": 0.001,
        "timestamp": 1790964000000,
    }


def _write_raw(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ============================================================
# Default State
# ============================================================

def test_default_state_valid():
    state = default_state(1000.0)
    validate_state(state)


def test_default_state_contains_metadata():
    state = default_state(1000.0)
    assert state["strategy_version"] == STRATEGY_CONFIG["version"]
    assert state["strategy_hash"] == compute_strategy_hash()


def test_default_state_equity_values():
    state = default_state(500.0)
    assert state["initial_equity"] == 500.0
    assert state["equity"] == 500.0
    assert state["peak_equity"] == 500.0


def test_default_state_negative_raises():
    try:
        default_state(-1.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_default_state_zero_raises():
    try:
        default_state(0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_default_state_bool_raises():
    try:
        default_state(True)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_default_state_string_raises():
    try:
        default_state("1000")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ============================================================
# Validate State
# ============================================================

def test_validate_missing_field():
    state = default_state()
    del state["equity"]
    try:
        validate_state(state)
        assert False
    except ValueError as exc:
        assert "equity" in str(exc)


def test_validate_wrong_version():
    state = default_state()
    state["version"] = "9.9.9"
    try:
        validate_state(state)
        assert False
    except ValueError as exc:
        assert "version" in str(exc).lower()


def test_validate_negative_equity():
    state = default_state()
    state["equity"] = -1.0
    try:
        validate_state(state)
        assert False
    except ValueError:
        pass


def test_validate_position_none_ok():
    validate_state(default_state())


def test_validate_position_dict_ok():
    state = default_state()
    state["position"] = {"side": "LONG"}
    validate_state(state)


def test_validate_position_bad_type():
    state = default_state()
    state["position"] = "not a dict"
    try:
        validate_state(state)
        assert False
    except ValueError:
        pass


def test_validate_cooldown_int():
    state = default_state()
    state["cooldown_until_index"] = 42
    validate_state(state)


def test_validate_cooldown_none():
    state = default_state()
    state["cooldown_until_index"] = None
    validate_state(state)


def test_validate_cooldown_bad_type():
    state = default_state()
    state["cooldown_until_index"] = "42"
    try:
        validate_state(state)
        assert False
    except ValueError:
        pass


def test_validate_last_candle_bad_type():
    state = default_state()
    state["last_processed_candle_ts"] = "yesterday"
    try:
        validate_state(state)
        assert False
    except ValueError:
        pass


# ============================================================
# Save / Load
# ============================================================

def test_save_and_load_roundtrip():
    path = _tmp_path(".json")
    try:
        original = default_state(2000.0)
        original["equity"] = 2100.0
        original["peak_equity"] = 2150.0
        original["daily_loss"] = 5.0
        save_state(path, original)

        loaded = load_state(path, strict=False)
        assert loaded == original
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_save_atomic_no_tmp_left():
    path = _tmp_path(".json")
    dir_ = os.path.dirname(path) or "."
    try:
        save_state(path, default_state())
        leftover = [
            f for f in os.listdir(dir_)
            if f.startswith(".state_") and f.endswith(".tmp")
        ]
        assert leftover == []
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_save_invalid_state_raises():
    path = _tmp_path(".json")
    try:
        state = default_state()
        del state["version"]
        try:
            save_state(path, state)
            assert False
        except ValueError:
            pass
        assert not os.path.exists(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_load_nonexistent_raises():
    path = "/tmp/nonexistent_state_xyz_99999.json"
    try:
        load_state(path)
        assert False
    except FileNotFoundError:
        pass


def test_load_corrupt_json_raises():
    path = _tmp_path(".json")
    try:
        with open(path, "w") as f:
            f.write("{not valid json")
        try:
            load_state(path)
            assert False
        except (json.JSONDecodeError, ValueError):
            pass
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_load_strict_hash_match():
    path = _tmp_path(".json")
    try:
        save_state(path, default_state())
        state = load_state(path, strict=True)
        assert state["strategy_hash"] == compute_strategy_hash()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_load_strict_hash_mismatch_raises():
    path = _tmp_path(".json")
    try:
        state = default_state()
        state["strategy_hash"] = "sha256:" + "0" * 64
        _write_raw(path, state)

        try:
            load_state(path, strict=True)
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "different strategy" in str(exc).lower() or "hash" in str(exc).lower()

        loaded = load_state(path, strict=False)
        assert loaded["strategy_hash"] == "sha256:" + "0" * 64
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_state_exists():
    path = _tmp_path(".json")
    try:
        assert not state_exists(path)
        save_state(path, default_state())
        assert state_exists(path)
        delete_state(path)
        assert not state_exists(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_load_or_init_creates():
    path = _tmp_path(".json")
    try:
        state = load_or_init_state(path, 1234.0)
        assert state["initial_equity"] == 1234.0
        assert os.path.exists(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_load_or_init_preserves():
    path = _tmp_path(".json")
    try:
        state = default_state(1000.0)
        state["equity"] = 1500.0
        save_state(path, state)
        loaded = load_or_init_state(path, 9999.0)
        assert loaded["equity"] == 1500.0
        assert loaded["initial_equity"] == 1000.0
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Trade Log — Basics
# ============================================================

def test_trade_log_creates_header():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry()])
        with open(path, "r", encoding="utf-8") as f:
            header = f.readline().strip()
        assert header == ",".join(TRADE_LOG_COLUMNS)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_trade_log_append_returns_counts():
    path = _tmp_path(".csv")
    try:
        n_app, n_sk = append_trade_events(path, [_sample_entry()])
        assert n_app == 1 and n_sk == 0
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_trade_log_multiple_events():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry(), _sample_exit()])
        rows = load_trade_log(path)
        assert len(rows) == 2
        assert rows[0]["event_type"] == "ENTRY"
        assert rows[1]["event_type"] == "EXIT"
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_trade_log_contains_metadata():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry()])
        rows = load_trade_log(path)
        assert rows[0]["strategy_version"] == STRATEGY_CONFIG["version"]
        assert rows[0]["strategy_hash"] == compute_strategy_hash()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_trade_log_timestamp_iso():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry()])
        rows = load_trade_log(path)
        assert rows[0]["timestamp_iso"].endswith("+00:00")
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Trade Log — Idempotency
# ============================================================

def test_dedup_same_event_twice():
    path = _tmp_path(".csv")
    try:
        ev = _sample_entry()
        append_trade_events(path, [ev])
        n_app, n_sk = append_trade_events(path, [ev])
        assert n_app == 0
        assert n_sk == 1
        rows = load_trade_log(path)
        assert len(rows) == 1
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_dedup_partial_batch():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry()])
        n_app, n_sk = append_trade_events(path, [
            _sample_entry(),
            _sample_exit(),
        ])
        assert n_app == 1
        assert n_sk == 1
        rows = load_trade_log(path)
        assert len(rows) == 2
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_dedup_differs_when_content_differs():
    path = _tmp_path(".csv")
    try:
        ev1 = _sample_entry()
        ev2 = dict(ev1)
        ev2["price"] = 99999.0
        append_trade_events(path, [ev1])
        n_app, _ = append_trade_events(path, [ev2])
        assert n_app == 1
        rows = load_trade_log(path)
        assert len(rows) == 2
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Trade Log — Event Seq
# ============================================================

def test_event_seq_monotonic():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry(), _sample_exit()])
        rows = load_trade_log(path)
        seqs = [int(r["event_seq"]) for r in rows]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_event_seq_continues_across_batches():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry()])
        append_trade_events(path, [_sample_exit()])
        rows = load_trade_log(path)
        seqs = [int(r["event_seq"]) for r in rows]
        assert seqs == [1, 2]
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Trade Log — Header Validation
# ============================================================

def test_load_header_mismatch_raises():
    path = _tmp_path(".csv")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("col_a,col_b\n1,2\n")
        try:
            load_trade_log(path, validate_header=True)
            assert False
        except ValueError as exc:
            assert "header mismatch" in str(exc).lower()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_load_header_validation_disabled():
    path = _tmp_path(".csv")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("col_a,col_b\n1,2\n")
        rows = load_trade_log(path, validate_header=False)
        assert len(rows) == 1
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Trade Log — Edge Cases
# ============================================================

def test_load_empty_file():
    path = "/tmp/nonexistent_log_xyz_99999.csv"
    assert load_trade_log(path) == []


def test_delete_trade_log():
    path = _tmp_path(".csv")
    try:
        append_trade_events(path, [_sample_entry()])
        assert trade_log_exists(path)
        delete_trade_log(path)
        assert not trade_log_exists(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_unknown_field_ignored():
    path = _tmp_path(".csv")
    try:
        ev = _sample_entry()
        ev["unknown_field_xyz"] = "ignored"
        append_trade_events(path, [ev])
        rows = load_trade_log(path)
        assert "unknown_field_xyz" not in rows[0]
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_bad_timestamp_raises():
    path = _tmp_path(".csv")
    try:
        ev = _sample_entry()
        ev["timestamp"] = "not a number"
        try:
            append_trade_events(path, [ev])
            assert False
        except ValueError as exc:
            assert "timestamp" in str(exc).lower()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_none_timestamp_ok():
    path = _tmp_path(".csv")
    try:
        ev = _sample_entry()
        ev["timestamp"] = None
        append_trade_events(path, [ev])
        rows = load_trade_log(path)
        assert rows[0]["timestamp_iso"] == ""
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_default_state_valid,
        test_default_state_contains_metadata,
        test_default_state_equity_values,
        test_default_state_negative_raises,
        test_default_state_zero_raises,
        test_default_state_bool_raises,
        test_default_state_string_raises,
        test_validate_missing_field,
        test_validate_wrong_version,
        test_validate_negative_equity,
        test_validate_position_none_ok,
        test_validate_position_dict_ok,
        test_validate_position_bad_type,
        test_validate_cooldown_int,
        test_validate_cooldown_none,
        test_validate_cooldown_bad_type,
        test_validate_last_candle_bad_type,
        test_save_and_load_roundtrip,
        test_save_atomic_no_tmp_left,
        test_save_invalid_state_raises,
        test_load_nonexistent_raises,
        test_load_corrupt_json_raises,
        test_load_strict_hash_match,
        test_load_strict_hash_mismatch_raises,
        test_state_exists,
        test_load_or_init_creates,
        test_load_or_init_preserves,
        test_trade_log_creates_header,
        test_trade_log_append_returns_counts,
        test_trade_log_multiple_events,
        test_trade_log_contains_metadata,
        test_trade_log_timestamp_iso,
        test_dedup_same_event_twice,
        test_dedup_partial_batch,
        test_dedup_differs_when_content_differs,
        test_event_seq_monotonic,
        test_event_seq_continues_across_batches,
        test_load_header_mismatch_raises,
        test_load_header_validation_disabled,
        test_load_empty_file,
        test_delete_trade_log,
        test_unknown_field_ignored,
        test_bad_timestamp_raises,
        test_none_timestamp_ok,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 State Manager Unit Tests")
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