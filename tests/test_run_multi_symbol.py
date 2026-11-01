"""Safety and behavioral tests for ApexQuant V2 runner."""

import copy
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

import run_multi_symbol as runner

from state_manager_v2 import default_state_v2


# ============================================================
# Fixtures
# ============================================================

def _make_klines(
    n=300,
    start_ts=1_700_000_000_000,
    interval_ms=3_600_000,
):
    return [
        [
            start_ts + index * interval_ms,
            "100",
            "105",
            "95",
            "102",
            "1000",
        ]
        for index in range(n)
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


def _fake_indicator_params():
    return {
        "ema_fast_p": 50,
        "ema_slow_p": 200,
        "donchian_p": 20,
        "atr_p": 14,
    }


# ============================================================
# State directory guard
# ============================================================

def test_guard_rejects_v1_state_dir():

    with pytest.raises(
        RuntimeError,
        match="must not use V1",
    ):

        runner._guard_state_dir(
            str(runner.V1_STATE_DIR)
        )


def test_guard_rejects_subdir_of_v1():

    subdir = (
        runner.V1_STATE_DIR
        / "v2_attempt"
    )

    with pytest.raises(
        RuntimeError,
        match="must not be inside V1",
    ):

        runner._guard_state_dir(
            str(subdir)
        )


def test_guard_accepts_separate_dir(tmp_path):

    result = runner._guard_state_dir(
        str(
            tmp_path
            / "multi_symbol_state_dir"
        )
    )

    assert result is not None


# ============================================================
# Whitelist
# ============================================================

def test_load_whitelist_preserves_order(tmp_path):

    whitelist = tmp_path / "wl.json"

    whitelist.write_text(
        '{"symbols": '
        '["SOL-SWAP-USDT", '
        '"BTC-SWAP-USDT", '
        '"ETH-SWAP-USDT"]}'
    )

    assert runner._load_whitelist_ordered(
        whitelist
    ) == [
        "SOL-SWAP-USDT",
        "BTC-SWAP-USDT",
        "ETH-SWAP-USDT",
    ]


def test_load_whitelist_rejects_missing(tmp_path):

    with pytest.raises(
        RuntimeError,
        match="not found",
    ):

        runner._load_whitelist_ordered(
            tmp_path / "nope.json"
        )


def test_load_whitelist_rejects_invalid_json(tmp_path):

    whitelist = tmp_path / "wl.json"

    whitelist.write_text(
        "not valid json {"
    )

    with pytest.raises(
        RuntimeError,
        match="not valid JSON",
    ):

        runner._load_whitelist_ordered(
            whitelist
        )


def test_load_whitelist_rejects_empty_list(tmp_path):

    whitelist = tmp_path / "wl.json"

    whitelist.write_text(
        '{"symbols": []}'
    )

    with pytest.raises(
        RuntimeError,
        match="non-empty",
    ):

        runner._load_whitelist_ordered(
            whitelist
        )


def test_load_whitelist_rejects_duplicate(tmp_path):

    whitelist = tmp_path / "wl.json"

    whitelist.write_text(
        '{"symbols": '
        '["BTC-SWAP-USDT", "BTC-SWAP-USDT"]}'
    )

    with pytest.raises(
        RuntimeError,
        match="Duplicate",
    ):

        runner._load_whitelist_ordered(
            whitelist
        )


def test_load_whitelist_rejects_wrong_symbol_format(tmp_path):

    whitelist = tmp_path / "wl.json"

    whitelist.write_text(
        '{"symbols": ["BTCUSDT"]}'
    )

    with pytest.raises(
        RuntimeError,
        match="Invalid symbol",
    ):

        runner._load_whitelist_ordered(
            whitelist
        )


# ============================================================
# SYMBOLS override
# ============================================================

def test_symbols_override_returns_subset(monkeypatch):

    monkeypatch.setenv(
        "SYMBOLS",
        "BTC-SWAP-USDT,ETH-SWAP-USDT",
    )

    state = default_state_v2(
        symbols=[
            "BTC-SWAP-USDT",
            "ETH-SWAP-USDT",
            "SOL-SWAP-USDT",
        ]
    )

    result = runner._resolve_symbols_override(
        [
            "BTC-SWAP-USDT",
            "ETH-SWAP-USDT",
            "SOL-SWAP-USDT",
        ],
        state,
    )

    assert result == [
        "BTC-SWAP-USDT",
        "ETH-SWAP-USDT",
    ]


def test_symbols_override_rejects_non_whitelisted(monkeypatch):

    monkeypatch.setenv(
        "SYMBOLS",
        "FAKE-SWAP-USDT",
    )

    state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    with pytest.raises(
        RuntimeError,
        match="non-whitelisted",
    ):

        runner._resolve_symbols_override(
            ["BTC-SWAP-USDT"],
            state,
        )


def test_symbols_override_rejects_missing_active(monkeypatch):

    monkeypatch.setenv(
        "SYMBOLS",
        "BTC-SWAP-USDT",
    )

    state = default_state_v2(
        symbols=[
            "BTC-SWAP-USDT",
            "ETH-SWAP-USDT",
        ]
    )

    state["symbols"]["ETH-SWAP-USDT"]["position"] = {
        "side": "LONG"
    }

    with pytest.raises(
        RuntimeError,
        match="excludes currently active",
    ):

        runner._resolve_symbols_override(
            [
                "BTC-SWAP-USDT",
                "ETH-SWAP-USDT",
            ],
            state,
        )


def test_symbols_override_noop_when_unset(monkeypatch):

    monkeypatch.delenv(
        "SYMBOLS",
        raising=False,
    )

    state = default_state_v2(
        symbols=[
            "BTC-SWAP-USDT",
            "ETH-SWAP-USDT",
        ]
    )

    result = runner._resolve_symbols_override(
        [
            "BTC-SWAP-USDT",
            "ETH-SWAP-USDT",
        ],
        state,
    )

    assert result == [
        "BTC-SWAP-USDT",
        "ETH-SWAP-USDT",
    ]


def test_symbols_override_rejects_empty_value(monkeypatch):

    monkeypatch.setenv(
        "SYMBOLS",
        " , ",
    )

    state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    with pytest.raises(
        RuntimeError,
        match="no valid symbols",
    ):

        runner._resolve_symbols_override(
            ["BTC-SWAP-USDT"],
            state,
        )


# ============================================================
# New candle indices
# ============================================================

def test_find_new_indices_all_when_none():

    klines = [
        [1000, "", "", "", ""],
        [2000, "", "", "", ""],
    ]

    assert runner._find_new_indices(
        klines,
        None,
    ) == [0, 1]


def test_find_new_indices_only_newer():

    klines = [
        [1000, "", "", "", ""],
        [2000, "", "", "", ""],
        [3000, "", "", "", ""],
    ]

    assert runner._find_new_indices(
        klines,
        1500,
    ) == [1, 2]


def test_find_new_indices_empty_klines():

    assert runner._find_new_indices(
        [],
        1000,
    ) == []


# ============================================================
# Legacy isolated processing safety
# ============================================================

def test_process_error_leaves_state_untouched(monkeypatch):

    state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    original_state = copy.deepcopy(
        state
    )

    klines = _make_klines(300)

    call_count = [0]

    def fake_process(
        state_copy,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        call_count[0] += 1

        if call_count[0] == 5:
            raise RuntimeError(
                "simulated mid-processing failure"
            )

        state_copy["equity"] -= 10

        state_copy["symbols"][symbol][
            "last_processed_candle_ts"
        ] = int(
            klines_[index][0]
        )

        return state_copy, []

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    new_state, events, error, n_processed = (
        runner._process_symbol_isolated(
            state,
            "BTC-SWAP-USDT",
            klines,
            _fake_specs(),
            {},
            {"BTC-SWAP-USDT"},
        )
    )

    assert error is not None
    assert "simulated" in error

    assert events == []
    assert n_processed == 0

    assert new_state == original_state
    assert state == original_state


# ============================================================
# Shared chronological processing
# ============================================================

def test_all_symbols_are_processed_chronologically(
    monkeypatch,
    tmp_path,
):

    symbols = [
        "BTC-SWAP-USDT",
        "ETH-SWAP-USDT",
    ]

    state = default_state_v2(
        symbols=symbols
    )

    btc_klines = _make_klines(
        2,
        start_ts=1_700_000_000_000,
    )

    eth_klines = _make_klines(
        2,
        start_ts=1_699_000_000_000,
    )

    monkeypatch.setattr(
        runner,
        "get_symbol_specs",
        lambda symbol: _fake_specs(),
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    processed = []

    def fake_process(
        state_v2,
        symbol,
        klines,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        timestamp = int(
            klines[index][0]
        )

        processed.append(
            (timestamp, symbol)
        )

        state_v2["symbols"][symbol][
            "last_processed_candle_ts"
        ] = timestamp

        return state_v2, []

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    save_calls = []

    monkeypatch.setattr(
        runner,
        "save_state_v2",
        lambda directory, current_state: (
            save_calls.append(
                copy.deepcopy(current_state)
            )
        ),
    )

    new_state, total_events, errors = (
        runner._process_all_symbols(
            state,
            symbols,
            {
                "BTC-SWAP-USDT": btc_klines,
                "ETH-SWAP-USDT": eth_klines,
            },
            {},
            set(symbols),
            str(tmp_path),
        )
    )

    assert processed == sorted(
        processed,
        key=lambda item: (
            item[0],
            symbols.index(item[1]),
        ),
    )

    assert len(processed) == 4
    assert total_events == 0
    assert errors == {}

    assert len(save_calls) == 1

    assert (
        new_state["symbols"]["BTC-SWAP-USDT"][
            "last_processed_candle_ts"
        ]
        == btc_klines[-1][0]
    )

    assert (
        new_state["symbols"]["ETH-SWAP-USDT"][
            "last_processed_candle_ts"
        ]
        == eth_klines[-1][0]
    )


def test_equal_timestamp_uses_whitelist_order(
    monkeypatch,
    tmp_path,
):

    symbols = [
        "ETH-SWAP-USDT",
        "BTC-SWAP-USDT",
    ]

    state = default_state_v2(
        symbols=symbols
    )

    shared_start = 1_700_000_000_000

    klines = {
        symbol: _make_klines(
            1,
            start_ts=shared_start,
        )
        for symbol in symbols
    }

    monkeypatch.setattr(
        runner,
        "get_symbol_specs",
        lambda symbol: _fake_specs(),
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    processed = []

    def fake_process(
        state_v2,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        processed.append(symbol)

        state_v2["symbols"][symbol][
            "last_processed_candle_ts"
        ] = int(
            klines_[index][0]
        )

        return state_v2, []

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    monkeypatch.setattr(
        runner,
        "save_state_v2",
        lambda *a, **kw: None,
    )

    runner._process_all_symbols(
        state,
        symbols,
        klines,
        {},
        set(symbols),
        str(tmp_path),
    )

    assert processed == symbols


def test_events_are_tagged_with_symbol_and_timestamp(
    monkeypatch,
    tmp_path,
):

    symbols = [
        "BTC-SWAP-USDT",
    ]

    state = default_state_v2(
        symbols=symbols
    )

    klines = _make_klines(
        1,
        start_ts=1_700_000_000_000,
    )

    monkeypatch.setattr(
        runner,
        "get_symbol_specs",
        lambda symbol: _fake_specs(),
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    captured = {}

    def fake_process(
        state_v2,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        return state_v2, [
            {
                "type": "TEST_EVENT",
                "timestamp": 123,
            }
        ]

    def fake_append(
        directory,
        events,
        strategy_hash,
    ):

        captured["events"] = events

        return len(events), 0

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    monkeypatch.setattr(
        runner,
        "append_events",
        fake_append,
    )

    monkeypatch.setattr(
        runner,
        "compute_strategy_hash",
        lambda: "sha256:test",
    )

    monkeypatch.setattr(
        runner,
        "save_state_v2",
        lambda *a, **kw: None,
    )

    _, total_events, errors = (
        runner._process_all_symbols(
            state,
            symbols,
            {
                "BTC-SWAP-USDT": klines,
            },
            {},
            set(symbols),
            str(tmp_path),
        )
    )

    assert errors == {}
    assert total_events == 1

    event = captured["events"][0]

    assert event["symbol"] == "BTC-SWAP-USDT"
    assert event["candle_ts_ms"] == klines[0][0]


def test_processing_failure_does_not_save_state(
    monkeypatch,
    tmp_path,
):

    symbols = [
        "BTC-SWAP-USDT",
    ]

    state = default_state_v2(
        symbols=symbols
    )

    original_state = copy.deepcopy(
        state
    )

    klines = _make_klines(2)

    monkeypatch.setattr(
        runner,
        "get_symbol_specs",
        lambda symbol: _fake_specs(),
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    def fake_process(
        state_v2,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        state_v2["equity"] -= 10

        if index == 1:
            raise RuntimeError(
                "simulated processing failure"
            )

        return state_v2, []

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    save_calls = []

    monkeypatch.setattr(
        runner,
        "save_state_v2",
        lambda *a, **kw: save_calls.append(True),
    )

    with pytest.raises(
        RuntimeError,
        match="Chronological processing failed",
    ):

        runner._process_all_symbols(
            state,
            symbols,
            {
                "BTC-SWAP-USDT": klines,
            },
            {},
            set(symbols),
            str(tmp_path),
        )

    assert state == original_state
    assert save_calls == []


# ============================================================
# Event append failure
# ============================================================

def test_append_failure_does_not_commit_state(
    monkeypatch,
    tmp_path,
):

    state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    original_state = copy.deepcopy(
        state
    )

    klines = _make_klines(1)

    monkeypatch.setattr(
        runner,
        "get_symbol_specs",
        lambda symbol: _fake_specs(),
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    def fake_process(
        state_v2,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        state_v2["equity"] -= 10

        state_v2["symbols"][symbol][
            "last_processed_candle_ts"
        ] = int(
            klines_[index][0]
        )

        return state_v2, [
            {
                "type": "TEST_EVENT",
                "timestamp": int(
                    klines_[index][0]
                ),
            }
        ]

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    def fake_append(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        runner,
        "append_events",
        fake_append,
    )

    monkeypatch.setattr(
        runner,
        "compute_strategy_hash",
        lambda: "sha256:test",
    )

    save_calls = []

    monkeypatch.setattr(
        runner,
        "save_state_v2",
        lambda directory, current_state: (
            save_calls.append(
                copy.deepcopy(current_state)
            )
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="append_events failed",
    ):

        runner._process_all_symbols(
            state,
            ["BTC-SWAP-USDT"],
            {
                "BTC-SWAP-USDT": klines,
            },
            {},
            {"BTC-SWAP-USDT"},
            str(tmp_path),
        )

    assert state == original_state
    assert save_calls == []


# ============================================================
# Active symbol fail-closed behavior
# ============================================================

def test_active_symbol_without_klines_aborts_cycle(
    monkeypatch,
    tmp_path,
):

    symbols = [
        "BTC-SWAP-USDT",
        "ETH-SWAP-USDT",
    ]

    state = default_state_v2(
        symbols=symbols
    )

    state["symbols"]["ETH-SWAP-USDT"]["position"] = {
        "side": "LONG",
    }

    monkeypatch.setattr(
        runner,
        "get_symbol_specs",
        lambda symbol: _fake_specs(),
    )

    with pytest.raises(
        RuntimeError,
        match="candle data is unavailable",
    ):

        runner._process_all_symbols(
            state,
            symbols,
            {
                "BTC-SWAP-USDT": _make_klines(1),
                "ETH-SWAP-USDT": None,
            },
            {},
            set(symbols),
            str(tmp_path),
        )


# ============================================================
# Catchup cap
# ============================================================

def test_catchup_caps_at_100(monkeypatch):

    state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    klines = _make_klines(250)

    call_count = [0]

    def fake_process(
        state_copy,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        call_count[0] += 1

        state_copy["symbols"][symbol][
            "last_processed_candle_ts"
        ] = int(
            klines_[index][0]
        )

        return state_copy, []

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    new_state, events, error, n_processed = (
        runner._process_symbol_isolated(
            state,
            "BTC-SWAP-USDT",
            klines,
            _fake_specs(),
            {},
            {"BTC-SWAP-USDT"},
        )
    )

    assert error is None
    assert n_processed == 100
    assert call_count[0] == 100

    expected_timestamp = (
        1_700_000_000_000
        + 99 * 3_600_000
    )

    assert (
        new_state["symbols"]["BTC-SWAP-USDT"][
            "last_processed_candle_ts"
        ]
        == expected_timestamp
    )


def test_catchup_resumes_next_cycle(monkeypatch):

    state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    klines = _make_klines(250)

    call_count = [0]

    def fake_process(
        state_copy,
        symbol,
        klines_,
        indicators,
        index,
        specs,
        config,
        signal,
        allowed_symbols,
    ):

        call_count[0] += 1

        state_copy["symbols"][symbol][
            "last_processed_candle_ts"
        ] = int(
            klines_[index][0]
        )

        return state_copy, []

    monkeypatch.setattr(
        runner,
        "compute_all",
        lambda *a, **kw: {},
    )

    monkeypatch.setattr(
        runner,
        "evaluate_signal",
        lambda indicators, index: None,
    )

    monkeypatch.setattr(
        runner,
        "process_symbol_candle",
        fake_process,
    )

    monkeypatch.setattr(
        runner,
        "get_indicator_params",
        _fake_indicator_params,
    )

    state1, _, error1, count1 = (
        runner._process_symbol_isolated(
            state,
            "BTC-SWAP-USDT",
            klines,
            _fake_specs(),
            {},
            {"BTC-SWAP-USDT"},
        )
    )

    assert error1 is None
    assert count1 == 100

    timestamp1 = state1["symbols"]["BTC-SWAP-USDT"][
        "last_processed_candle_ts"
    ]

    state2, _, error2, count2 = (
        runner._process_symbol_isolated(
            state1,
            "BTC-SWAP-USDT",
            klines,
            _fake_specs(),
            {},
            {"BTC-SWAP-USDT"},
        )
    )

    assert error2 is None
    assert count2 == 100

    timestamp2 = state2["symbols"]["BTC-SWAP-USDT"][
        "last_processed_candle_ts"
    ]

    assert timestamp2 > timestamp1


# ============================================================
# Main: reconciler must block processing
# ============================================================

def test_main_reconciler_error_returns_exit_failed(
    monkeypatch,
    tmp_path,
):

    monkeypatch.setenv(
        "STATE_DIR",
        str(tmp_path / "multi_symbol_state_dir"),
    )

    monkeypatch.setattr(
        runner,
        "V1_STATE_DIR",
        tmp_path / "paper_state_dir",
    )

    whitelist = (
        tmp_path
        / "crypto_whitelist.json"
    )

    whitelist.write_text(
        '{"symbols": ["BTC-SWAP-USDT"]}'
    )

    monkeypatch.setattr(
        runner,
        "CRYPTO_WHITELIST_PATH",
        whitelist,
    )

    initial_state = default_state_v2(
        symbols=["BTC-SWAP-USDT"]
    )

    monkeypatch.setattr(
        runner,
        "state_exists_v2",
        lambda directory: False,
    )

    monkeypatch.setattr(
        runner,
        "load_or_init_state_v2",
        lambda directory, **kwargs: initial_state,
    )

    monkeypatch.setattr(
        runner,
        "load_state_v2",
        lambda directory, **kwargs: initial_state,
    )

    monkeypatch.setattr(
        runner,
        "save_state_v2",
        lambda directory, current_state: None,
    )

    monkeypatch.setattr(
        runner,
        "fetch_closed_klines",
        lambda symbol, interval, limit=300: _make_klines(300),
    )

    monkeypatch.setattr(
        runner,
        "reconcile_with_data_v2",
        lambda *a, **kw: {
            "status": "ERROR",
            "checks": [],
            "summary": "test error",
        },
    )

    process_calls = [0]

    def fake_process_all(*args, **kwargs):
        process_calls[0] += 1
        return args[0], 0, {}

    monkeypatch.setattr(
        runner,
        "_process_all_symbols",
        fake_process_all,
    )

    monkeypatch.setattr(
        runner,
        "get_engine_config",
        lambda: {},
    )

    monkeypatch.setattr(
        runner,
        "get_fee_config",
        lambda: {
            "open_taker_fee": 0,
            "close_taker_fee": 0,
        },
    )

    result = runner.main()

    assert result == runner.EXIT_FAILED
    assert process_calls[0] == 0


def test_main_guard_failure_returns_exit_failed(
    monkeypatch,
    tmp_path,
):

    monkeypatch.setattr(
        runner,
        "V1_STATE_DIR",
        tmp_path / "paper_state_dir",
    )

    monkeypatch.setenv(
        "STATE_DIR",
        str(tmp_path / "paper_state_dir"),
    )

    result = runner.main()

    assert result == runner.EXIT_FAILED


def test_main_whitelist_parse_error_returns_exit_failed(
    monkeypatch,
    tmp_path,
):

    monkeypatch.setenv(
        "STATE_DIR",
        str(tmp_path / "multi_symbol_state_dir"),
    )

    monkeypatch.setattr(
        runner,
        "V1_STATE_DIR",
        tmp_path / "paper_state_dir",
    )

    whitelist = (
        tmp_path
        / "crypto_whitelist.json"
    )

    whitelist.write_text(
        "invalid json"
    )

    monkeypatch.setattr(
        runner,
        "CRYPTO_WHITELIST_PATH",
        whitelist,
    )

    result = runner.main()

    assert result == runner.EXIT_FAILED


# ============================================================
# Exit code contract
# ============================================================

def test_exit_codes_documented():

    assert runner.EXIT_OK == 0
    assert runner.EXIT_FAILED == 1
    assert runner.EXIT_PARTIAL == 2


def test_max_catchup_bounded():

    assert (
        1
        <= runner.MAX_CATCHUP_CANDLES
        <= 500
    )