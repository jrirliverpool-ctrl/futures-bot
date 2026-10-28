"""tests/test_run_multi_symbol.py — Safety + behavioral tests for V2 runner.

Focus:
  - state_dir guard (rejects V1 and subdirs)
  - whitelist order + parse error handling
  - SYMBOLS override with fail-closed on missing active symbols
  - reconciler ERROR blocks all processing (behavioral)
  - per-symbol isolation: mid-process error → state unchanged
  - append failure → state NOT committed (rollback)
  - catchup cap enforced (behavioral)
  - resumption after catchup
  - exit code contract
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import run_multi_symbol as runner
from state_manager_v2 import default_state_v2


# ============================================================
# Fixtures
# ============================================================

def _make_klines(n=300, start_ts=1_700_000_000_000, interval_ms=3_600_000):
    return [
        [start_ts + i * interval_ms, "100", "105", "95", "102", "1000"]
        for i in range(n)
    ]


def _fake_specs():
    return {
        "contract_multiplier": 0.001,
        "min_qty": 0.0001,
        "max_qty": 1000.0,
        "qty_step": 0.0001,
        "min_notional": 0.0,
        "max_leverage": 20,
    }


# ============================================================
# Guard
# ============================================================

def test_guard_rejects_v1_state_dir():
    with pytest.raises(RuntimeError, match="must not use V1"):
        runner._guard_state_dir(str(runner.V1_STATE_DIR))


def test_guard_rejects_subdir_of_v1():
    subdir = runner.V1_STATE_DIR / "v2_attempt"
    with pytest.raises(RuntimeError, match="must not be inside V1"):
        runner._guard_state_dir(str(subdir))


def test_guard_accepts_separate_dir(tmp_path):
    assert runner._guard_state_dir(str(tmp_path / "multi_symbol_state_dir")) is not None


# ============================================================
# Whitelist
# ============================================================

def test_load_whitelist_preserves_order(tmp_path):
    wl = tmp_path / "wl.json"
    wl.write_text('{"symbols": ["SOL-SWAP-USDT", "BTC-SWAP-USDT", "ETH-SWAP-USDT"]}')
    assert runner._load_whitelist_ordered(wl) == [
        "SOL-SWAP-USDT", "BTC-SWAP-USDT", "ETH-SWAP-USDT"
    ]


def test_load_whitelist_rejects_missing(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        runner._load_whitelist_ordered(tmp_path / "nope.json")


def test_load_whitelist_rejects_invalid_json(tmp_path):
    wl = tmp_path / "wl.json"
    wl.write_text("not valid json {")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        runner._load_whitelist_ordered(wl)


def test_load_whitelist_rejects_empty_list(tmp_path):
    wl = tmp_path / "wl.json"
    wl.write_text('{"symbols": []}')
    with pytest.raises(RuntimeError, match="non-empty"):
        runner._load_whitelist_ordered(wl)


def test_load_whitelist_rejects_duplicate(tmp_path):
    wl = tmp_path / "wl.json"
    wl.write_text('{"symbols": ["BTC-SWAP-USDT", "BTC-SWAP-USDT"]}')
    with pytest.raises(RuntimeError, match="Duplicate"):
        runner._load_whitelist_ordered(wl)


# ============================================================
# SYMBOLS override
# ============================================================

def test_symbols_override_returns_subset(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC-SWAP-USDT,ETH-SWAP-USDT")
    state = default_state_v2(symbols=["BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT"])
    result = runner._resolve_symbols_override(
        ["BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT"], state,
    )
    assert result == ["BTC-SWAP-USDT", "ETH-SWAP-USDT"]


def test_symbols_override_rejects_non_whitelisted(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "FAKE-SWAP-USDT")
    state = default_state_v2(symbols=["BTC-SWAP-USDT"])
    with pytest.raises(RuntimeError, match="non-whitelisted"):
        runner._resolve_symbols_override(["BTC-SWAP-USDT"], state)


def test_symbols_override_rejects_missing_active(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC-SWAP-USDT")
    state = default_state_v2(symbols=["BTC-SWAP-USDT", "ETH-SWAP-USDT"])
    state["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    with pytest.raises(RuntimeError, match="excludes currently active"):
        runner._resolve_symbols_override(
            ["BTC-SWAP-USDT", "ETH-SWAP-USDT"], state,
        )


def test_symbols_override_noop_when_unset(monkeypatch):
    monkeypatch.delenv("SYMBOLS", raising=False)
    state = default_state_v2(symbols=["BTC-SWAP-USDT", "ETH-SWAP-USDT"])
    result = runner._resolve_symbols_override(
        ["BTC-SWAP-USDT", "ETH-SWAP-USDT"], state,
    )
    assert result == ["BTC-SWAP-USDT", "ETH-SWAP-USDT"]


# ============================================================
# _find_new_indices
# ============================================================

def test_find_new_indices_all_when_none():
    klines = [[1000, "", "", "", ""], [2000, "", "", "", ""]]
    assert runner._find_new_indices(klines, None) == [0, 1]


def test_find_new_indices_only_newer():
    klines = [[1000, "", "", "", ""], [2000, "", "", "", ""], [3000, "", "", "", ""]]
    assert runner._find_new_indices(klines, 1500) == [1, 2]


def test_find_new_indices_empty_klines():
    assert runner._find_new_indices([], 1000) == []


# ============================================================
# Per-symbol isolation: mid-process error → state unchanged
# ============================================================

def test_process_error_leaves_state_untouched(monkeypatch):
    state = default_state_v2(symbols=["BTC-SWAP-USDT"])
    initial_equity = state["equity"]
    initial_lpct = state["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"]
    klines = _make_klines(300)

    call_count = [0]
    def fake_process(state_copy, symbol, klines_, ind, idx, specs, cfg, signal, allowed_symbols):
        call_count[0] += 1
        if call_count[0] == 5:
            raise RuntimeError("simulated mid-processing failure")
        # These mutations must NOT leak to original state on error
        state_copy["equity"] -= 10
        state_copy["symbols"][symbol]["last_processed_candle_ts"] = int(klines_[idx][0])
        return state_copy, []

    monkeypatch.setattr(runner, "compute_all", lambda *a, **kw: {})
    monkeypatch.setattr(runner, "evaluate_signal", lambda ind, idx: None)
    monkeypatch.setattr(runner, "process_symbol_candle", fake_process)
    monkeypatch.setattr(runner, "get_indicator_params", lambda: {
        "ema_fast_p": 50, "ema_slow_p": 200, "donchian_p": 20, "atr_p": 14,
    })

    new_state, events, err, n_processed = runner._process_symbol_isolated(
        state, "BTC-SWAP-USDT", klines, _fake_specs(), {},
        {"BTC-SWAP-USDT"},
    )

    assert err is not None
    assert "simulated" in err
    assert events == []
    assert n_processed == 0
    # Original state completely unchanged
    assert new_state["equity"] == initial_equity
    assert new_state["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] == initial_lpct


# ============================================================
# Per-symbol isolation: append failure → state not committed
# ============================================================

def test_append_failure_does_not_commit_state(monkeypatch, tmp_path):
    state = default_state_v2(symbols=["BTC-SWAP-USDT"])
    initial_lpct = state["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"]

    def fake_isolated(state_v2, sym, klines, specs, config, allowed):
        new_state = copy.deepcopy(state_v2)
        new_state["symbols"][sym]["last_processed_candle_ts"] = 9999
        return new_state, [{"type": "SIGNAL", "timestamp": 1000}], None, 1

    def fake_append(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runner, "_process_symbol_isolated", fake_isolated)
    monkeypatch.setattr(runner, "append_events", fake_append)
    monkeypatch.setattr(runner, "compute_strategy_hash", lambda: "sha256:test")
    monkeypatch.setattr(runner, "get_symbol_specs", lambda s: _fake_specs())
    monkeypatch.setattr(runner, "save_state_v2", lambda d, s: None)

    new_state, total_events, errors = runner._process_all_symbols(
        state, ["BTC-SWAP-USDT"], {"BTC-SWAP-USDT": _make_klines(300)},
        {}, {"BTC-SWAP-USDT"}, str(tmp_path),
    )

    assert "BTC-SWAP-USDT" in errors
    assert "disk full" in errors["BTC-SWAP-USDT"]
    assert total_events == 0
    # State NOT committed
    assert new_state["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] == initial_lpct


# ============================================================
# Catchup cap (behavioral)
# ============================================================

def test_catchup_caps_at_100(monkeypatch):
    state = default_state_v2(symbols=["BTC-SWAP-USDT"])
    klines = _make_klines(250)  # 250 new candles, lpct=None

    call_count = [0]
    def fake_process(state_copy, symbol, klines_, ind, idx, specs, cfg, signal, allowed_symbols):
        call_count[0] += 1
        state_copy["symbols"][symbol]["last_processed_candle_ts"] = int(klines_[idx][0])
        return state_copy, []

    monkeypatch.setattr(runner, "compute_all", lambda *a, **kw: {})
    monkeypatch.setattr(runner, "evaluate_signal", lambda ind, idx: None)
    monkeypatch.setattr(runner, "process_symbol_candle", fake_process)
    monkeypatch.setattr(runner, "get_indicator_params", lambda: {
        "ema_fast_p": 50, "ema_slow_p": 200, "donchian_p": 20, "atr_p": 14,
    })

    new_state, events, err, n_processed = runner._process_symbol_isolated(
        state, "BTC-SWAP-USDT", klines, _fake_specs(), {},
        {"BTC-SWAP-USDT"},
    )

    assert err is None
    assert n_processed == 100
    assert call_count[0] == 100
    # last_processed = 100th candle's ts (index 99)
    assert new_state["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] == 1_700_000_000_000 + 99 * 3_600_000


def test_catchup_resumes_next_cycle(monkeypatch):
    """After processing 100 of 250, next call processes the next 100."""
    state = default_state_v2(symbols=["BTC-SWAP-USDT"])
    klines = _make_klines(250)

    call_count = [0]
    def fake_process(state_copy, symbol, klines_, ind, idx, specs, cfg, signal, allowed_symbols):
        call_count[0] += 1
        state_copy["symbols"][symbol]["last_processed_candle_ts"] = int(klines_[idx][0])
        return state_copy, []

    monkeypatch.setattr(runner, "compute_all", lambda *a, **kw: {})
    monkeypatch.setattr(runner, "evaluate_signal", lambda ind, idx: None)
    monkeypatch.setattr(runner, "process_symbol_candle", fake_process)
    monkeypatch.setattr(runner, "get_indicator_params", lambda: {
        "ema_fast_p": 50, "ema_slow_p": 200, "donchian_p": 20, "atr_p": 14,
    })

    # Cycle 1
    state1, _, err1, n1 = runner._process_symbol_isolated(
        state, "BTC-SWAP-USDT", klines, _fake_specs(), {}, {"BTC-SWAP-USDT"},
    )
    assert err1 is None
    assert n1 == 100
    lpct_after_cycle1 = state1["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"]

    # Cycle 2
    state2, _, err2, n2 = runner._process_symbol_isolated(
        state1, "BTC-SWAP-USDT", klines, _fake_specs(), {}, {"BTC-SWAP-USDT"},
    )
    assert err2 is None
    assert n2 == 100
    lpct_after_cycle2 = state2["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"]
    assert lpct_after_cycle2 > lpct_after_cycle1


# ============================================================
# Reconciler ERROR blocks processing (behavioral)
# ============================================================

def test_main_reconciler_error_returns_exit_failed(monkeypatch, tmp_path):
    """When reconciler returns ERROR, main() returns EXIT_FAILED and
    _process_all_symbols is NEVER called."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "multi_symbol_state_dir"))
    monkeypatch.setattr(runner, "V1_STATE_DIR", tmp_path / "paper_state_dir")

    wl = tmp_path / "crypto_whitelist.json"
    wl.write_text('{"symbols": ["BTC-SWAP-USDT"]}')
    monkeypatch.setattr(runner, "CRYPTO_WHITELIST_PATH", wl)

    initial_state = default_state_v2(symbols=["BTC-SWAP-USDT"])
    monkeypatch.setattr(runner, "state_exists_v2", lambda d: False)
    monkeypatch.setattr(runner, "load_or_init_state_v2", lambda d, **kw: initial_state)
    monkeypatch.setattr(runner, "load_state_v2", lambda d, **kw: initial_state)
    monkeypatch.setattr(runner, "save_state_v2", lambda d, s: None)
    monkeypatch.setattr(
        runner, "fetch_closed_klines",
        lambda s, i, limit=300: _make_klines(300),
    )
    monkeypatch.setattr(
        runner, "reconcile_with_data_v2",
        lambda *a, **kw: {"status": "ERROR", "checks": [], "summary": "test error"},
    )

    process_calls = [0]
    def fake_process_all(*a, **kw):
        process_calls[0] += 1
        return a[0], 0, {}
    monkeypatch.setattr(runner, "_process_all_symbols", fake_process_all)

    monkeypatch.setattr(runner, "get_engine_config", lambda: {})
    monkeypatch.setattr(runner, "get_fee_config", lambda: {
        "open_taker_fee": 0, "close_taker_fee": 0,
    })

    rc = runner.main()
    assert rc == runner.EXIT_FAILED
    assert process_calls[0] == 0  # NEVER called


def test_main_guard_failure_returns_exit_failed(monkeypatch, tmp_path):
    """If state_dir is V1 or subdir, main() returns EXIT_FAILED."""
    monkeypatch.setattr(runner, "V1_STATE_DIR", tmp_path / "paper_state_dir")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "paper_state_dir"))

    rc = runner.main()
    assert rc == runner.EXIT_FAILED


def test_main_whitelist_parse_error_returns_exit_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "multi_symbol_state_dir"))
    monkeypatch.setattr(runner, "V1_STATE_DIR", tmp_path / "paper_state_dir")

    wl = tmp_path / "crypto_whitelist.json"
    wl.write_text("invalid json")
    monkeypatch.setattr(runner, "CRYPTO_WHITELIST_PATH", wl)

    rc = runner.main()
    assert rc == runner.EXIT_FAILED


# ============================================================
# Exit code contract
# ============================================================

def test_exit_codes_documented():
    assert runner.EXIT_OK == 0
    assert runner.EXIT_FAILED == 1
    assert runner.EXIT_PARTIAL == 2


def test_max_catchup_bounded():
    assert 1 <= runner.MAX_CATCHUP_CANDLES <= 500