"""tests/test_reconciler_v2.py — Unit tests for V2 reconciler.

All network calls are mocked. Tests never hit real Toobit API.

Tests verify that reconciler:
  - Never mutates the input state.
  - Returns ERROR (not exception) for malformed inputs.
  - Handles NaN/Inf in equity/peak.
  - Derives capacity from max_concurrent_positions.
  - Requires active symbols to have data.
  - Validates klines structure.
  - Reports known-vs-unknown interval BEFORE klines sufficiency.
"""

import copy
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import reconciler_v2 as rec
from state_manager_v2 import default_state_v2


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def state_v2():
    return default_state_v2(
        initial_equity=984.67,
        symbols=["BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT"],
        max_concurrent_positions=1,
    )


def _klines(n=300, start_ts=1_700_000_000_000, interval_ms=3_600_000):
    return [
        [start_ts + i * interval_ms, "100", "105", "95", "102", "1000"]
        for i in range(n)
    ]


def _last_ts(klines):
    return int(klines[-1][0])


def _checks_by_name(result):
    return {c["name"]: c for c in result["checks"]}


def _state_hash(state):
    return json.dumps(state, sort_keys=True, default=str)


# ============================================================
# Immutability
# ============================================================

def test_reconcile_portfolio_does_not_mutate_state(state_v2):
    before = _state_hash(state_v2)
    rec.reconcile_portfolio(state_v2)
    assert _state_hash(state_v2) == before


def test_reconcile_with_data_does_not_mutate_state_or_klines(state_v2):
    klines = _klines(300)
    klines_map = {sym: copy.deepcopy(klines) for sym in state_v2["symbols"]}
    before_state = _state_hash(state_v2)
    before_klines = {k: _state_hash(v) for k, v in klines_map.items()}

    now_ms = _last_ts(klines) + 3_600_000
    rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)

    assert _state_hash(state_v2) == before_state
    for k, v in klines_map.items():
        assert _state_hash(v) == before_klines[k]


# ============================================================
# Portfolio structure
# ============================================================

def test_portfolio_rejects_non_dict_state():
    r = rec.reconcile_portfolio("not a dict")
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["portfolio_state_is_dict"]["ok"]


def test_portfolio_rejects_non_dict_symbols(state_v2):
    state_v2["symbols"] = "not a dict"
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["portfolio_symbols_is_dict"]["ok"]


def test_portfolio_rejects_empty_symbols(state_v2):
    state_v2["symbols"] = {}
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["portfolio_symbols_non_empty"]["ok"]


def test_portfolio_rejects_invalid_max_concurrent(state_v2):
    for bad in [0, -1, True, "1", 1.5, None]:
        state_v2["max_concurrent_positions"] = bad
        r = rec.reconcile_portfolio(state_v2)
        assert r["status"] == rec.STATUS_ERROR, f"failed for {bad!r}"


# ============================================================
# Equity / peak (NaN, Inf, negative)
# ============================================================

def test_portfolio_detects_negative_equity(state_v2):
    state_v2["equity"] = -1.0
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


def test_portfolio_detects_nan_equity(state_v2):
    state_v2["equity"] = float("nan")
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["portfolio_equity_finite_non_negative"]["ok"]


def test_portfolio_detects_inf_equity(state_v2):
    state_v2["equity"] = float("inf")
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


def test_portfolio_detects_nan_peak(state_v2):
    state_v2["peak_equity"] = float("nan")
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


def test_portfolio_detects_inf_initial(state_v2):
    state_v2["initial_equity"] = float("inf")
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


def test_portfolio_detects_peak_below_initial(state_v2):
    state_v2["peak_equity"] = state_v2["initial_equity"] - 10.0
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


# ============================================================
# Capacity
# ============================================================

def test_portfolio_capacity_ok_when_empty(state_v2):
    r = rec.reconcile_portfolio(state_v2)
    names = _checks_by_name(r)
    assert names["portfolio_capacity_within_limit"]["ok"]


def test_portfolio_capacity_exceeded_by_positions(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    state_v2["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["portfolio_capacity_within_limit"]["ok"]


def test_portfolio_capacity_exceeded_by_mixed(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


def test_portfolio_capacity_derived_from_config(state_v2):
    """With max_concurrent=2, two positions should be allowed."""
    state_v2["max_concurrent_positions"] = 2
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    state_v2["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    r = rec.reconcile_portfolio(state_v2)
    names = _checks_by_name(r)
    assert names["portfolio_capacity_within_limit"]["ok"]


# ============================================================
# Exclusive state
# ============================================================

def test_portfolio_detects_position_and_pending_same_symbol(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["portfolio_symbols_exclusive_state"]["ok"]


# ============================================================
# Circuit breaker reason
# ============================================================

def test_portfolio_errors_when_cb_active_without_reason(state_v2):
    state_v2["circuit_breaker_active"] = True
    state_v2["circuit_breaker_reason"] = None
    r = rec.reconcile_portfolio(state_v2)
    assert r["status"] == rec.STATUS_ERROR


def test_portfolio_ok_when_cb_active_with_reason(state_v2):
    state_v2["circuit_breaker_active"] = True
    state_v2["circuit_breaker_reason"] = "DAILY_LOSS"
    r = rec.reconcile_portfolio(state_v2)
    names = _checks_by_name(r)
    assert names["portfolio_cb_reason_when_active"]["ok"]


# ============================================================
# Klines structure
# ============================================================

def test_klines_structure_accepts_valid(state_v2):
    klines = _klines(300)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    for sym in state_v2["symbols"]:
        state_v2["symbols"][sym]["last_processed_candle_ts"] = _last_ts(klines)
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    names = _checks_by_name(r)
    for sym in state_v2["symbols"]:
        assert names[f"symbol_{sym}_klines_structure"]["ok"]


def test_klines_structure_rejects_non_list(state_v2):
    klines_map = {sym: "not a list" for sym in state_v2["symbols"]}
    now_ms = 1_700_000_000_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["symbol_BTC-SWAP-USDT_klines_structure"]["ok"]


def test_klines_structure_rejects_short_candle(state_v2):
    klines = [[1_700_000_000_000, "100", "105", "95"]]  # only 4 elements
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = 1_700_000_000_000 + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR


def test_klines_structure_rejects_unsorted(state_v2):
    klines = _klines(300)
    # Swap two candles to break ordering
    klines[10], klines[11] = klines[11], klines[10]
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    assert not _checks_by_name(r)["symbol_BTC-SWAP-USDT_klines_structure"]["ok"]


def test_klines_structure_rejects_duplicates(state_v2):
    klines = _klines(300)
    klines[11] = list(klines[10])  # duplicate ts
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR


def test_klines_structure_rejects_non_int_ts(state_v2):
    klines = _klines(300)
    klines[5] = ["not_an_int", "100", "105", "95", "102", "1000"]
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR


# ============================================================
# Interval
# ============================================================

def test_unknown_interval_reported_even_with_no_klines(state_v2):
    """Interval error must be reported BEFORE klines sufficiency."""
    klines_map = {sym: [] for sym in state_v2["symbols"]}
    now_ms = 1_700_000_000_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "invalid_tf", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    check = _checks_by_name(r)["symbol_BTC-SWAP-USDT_interval_known"]
    assert not check["ok"]


def test_known_interval_recorded(state_v2):
    klines = _klines(300)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    for sym in state_v2["symbols"]:
        state_v2["symbols"][sym]["last_processed_candle_ts"] = _last_ts(klines)
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert _checks_by_name(r)["symbol_BTC-SWAP-USDT_interval_known"]["ok"]


# ============================================================
# Active vs idle
# ============================================================

def test_idle_symbol_insufficient_klines_is_warning(state_v2):
    klines = _klines(100)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    check = _checks_by_name(r)["symbol_BTC-SWAP-USDT_klines_sufficient"]
    assert not check["ok"]
    assert check["level"] == "warning"


def test_active_symbol_insufficient_klines_is_error(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    klines = _klines(100)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    check = _checks_by_name(r)["symbol_BTC-SWAP-USDT_klines_sufficient"]
    assert check["level"] == "error"


def test_idle_symbol_missing_klines_is_warning(state_v2):
    klines = _klines(300)
    klines_map = {"BTC-SWAP-USDT": klines, "SOL-SWAP-USDT": klines}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    check = _checks_by_name(r)["symbol_ETH-SWAP-USDT_klines_available"]
    assert not check["ok"]
    assert check["level"] == "warning"


def test_active_symbol_missing_klines_is_error(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    klines = _klines(300)
    klines_map = {"ETH-SWAP-USDT": klines, "SOL-SWAP-USDT": klines}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    check = _checks_by_name(r)["symbol_BTC-SWAP-USDT_klines_available"]
    assert check["level"] == "error"


def test_pending_entry_symbol_without_klines_is_error(state_v2):
    state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    klines = _klines(300)
    klines_map = {"BTC-SWAP-USDT": klines, "SOL-SWAP-USDT": klines}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    check = _checks_by_name(r)["symbol_ETH-SWAP-USDT_klines_available"]
    assert check["level"] == "error"


# ============================================================
# last_processed and gaps
# ============================================================

def test_last_processed_none_idle_is_warning(state_v2):
    klines = _klines(300)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    # lpct stays None for all
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    names = _checks_by_name(r)
    check = names["symbol_BTC-SWAP-USDT_last_processed_present"]
    assert not check["ok"]
    assert check["level"] == "warning"


def test_last_processed_none_active_is_error(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    klines = _klines(300)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR
    check = _checks_by_name(r)["symbol_BTC-SWAP-USDT_last_processed_present"]
    assert check["level"] == "error"


def test_last_processed_future_is_error(state_v2):
    klines = _klines(300)
    state_v2["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] = (
        _last_ts(klines) + 3_600_000
    )
    for sym in ("ETH-SWAP-USDT", "SOL-SWAP-USDT"):
        state_v2["symbols"][sym]["last_processed_candle_ts"] = _last_ts(klines)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_ERROR


def test_gap_warning_at_threshold(state_v2):
    klines = _klines(300)
    gap_candles = 5
    state_v2["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] = (
        _last_ts(klines) - gap_candles * 3_600_000
    )
    for sym in ("ETH-SWAP-USDT", "SOL-SWAP-USDT"):
        state_v2["symbols"][sym]["last_processed_candle_ts"] = _last_ts(klines)
    klines_map = {sym: klines for sym in state_v2["symbols"]}
    now_ms = _last_ts(klines) + 3_600_000
    r = rec.reconcile_with_data_v2(state_v2, klines_map, "1h", now_ms)
    assert r["status"] == rec.STATUS_WARNING
    check = _checks_by_name(r)["symbol_BTC-SWAP-USDT_no_significant_gap"]
    assert not check["ok"]
    assert check["level"] == "warning"


# ============================================================
# reconcile_public_v2
# ============================================================

def test_public_ok_when_all_healthy(state_v2, monkeypatch):
    klines = _klines(300)
    for sym in state_v2["symbols"]:
        state_v2["symbols"][sym]["last_processed_candle_ts"] = _last_ts(klines)

    monkeypatch.setattr(rec, "load_state_v2", lambda _d: state_v2)
    monkeypatch.setattr(rec, "fetch_klines", lambda _s, _i, limit=300: klines)

    r = rec.reconcile_public_v2("/tmp/fake", list(state_v2["symbols"]), "1h")
    assert r["status"] == rec.STATUS_OK


def test_public_error_on_state_load(state_v2, monkeypatch):
    def _raise(_d):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(rec, "load_state_v2", _raise)
    r = rec.reconcile_public_v2("/tmp/fake", ["BTC-SWAP-USDT"], "1h")
    assert r["status"] == rec.STATUS_ERROR


def test_public_warns_on_fetch_failure_idle(state_v2, monkeypatch):
    def _raise(_s, _i, limit=300):
        raise ConnectionError("net")

    monkeypatch.setattr(rec, "load_state_v2", lambda _d: state_v2)
    monkeypatch.setattr(rec, "fetch_klines", _raise)

    r = rec.reconcile_public_v2("/tmp/fake", list(state_v2["symbols"]), "1h")
    # idle symbols → fetch failures are warnings, klines_available warnings
    # BUT: idle + last_processed is None → warning; no ERROR
    assert r["status"] == rec.STATUS_WARNING


def test_public_error_on_fetch_failure_active(state_v2, monkeypatch):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}

    def _raise(_s, _i, limit=300):
        raise ConnectionError("net")

    monkeypatch.setattr(rec, "load_state_v2", lambda _d: state_v2)
    monkeypatch.setattr(rec, "fetch_klines", _raise)

    r = rec.reconcile_public_v2("/tmp/fake", list(state_v2["symbols"]), "1h")
    assert r["status"] == rec.STATUS_ERROR


# ============================================================
# Documented contract: ERROR must block trading
# ============================================================

def test_error_status_is_the_blocking_signal():
    """Documents the caller contract.

    In run_multi_symbol.py, any cycle where reconcile_public_v2 returns
    status=="ERROR" MUST be aborted — no entries, no exits, no
    notifications except an alert.

    This test does not execute the caller (not yet implemented), but
    ensures the constant stays the string callers will check against.
    If someone changes STATUS_ERROR to another string, this test fails
    so we remember to update callers.
    """
    assert rec.STATUS_ERROR == "ERROR"
    assert rec.STATUS_WARNING == "WARNING"
    assert rec.STATUS_OK == "OK"