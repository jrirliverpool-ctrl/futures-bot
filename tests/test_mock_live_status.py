"""Tests for MOCK_LIVE status reporting and safe finalization."""

import copy
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import run_mock_live as rm


BASE_TS = 1_698_796_800_000
HOUR = 3_600_000

FAKE_SPECS = {
    "symbol": "BTC-SWAP-USDT",
    "contract_multiplier": 0.001,
    "min_qty": 1.0,
    "max_qty": 1_000_000.0,
    "qty_step": 1.0,
    "min_notional": 5.0,
    "max_leverage": 100.0,
}

FAKE_HASH = "sha256:" + "a" * 64

REQUIRED_FIELDS = {
    "schema_version",
    "run_started_at_iso",
    "run_finished_at_iso",
    "run_status",
    "run_exit_code",
    "strategy_hash",
    "state_dir",
    "symbol",
    "interval",
    "reconciler_status",
    "reconciler_summary",
    "last_processed_candle_ts",
    "candles_processed_this_run",
    "catchup_lag",
    "total_new_detected",
    "equity",
    "peak_equity",
    "has_position",
    "error_message",
}


def make_candles(count, start_ts=BASE_TS):
    return [
        [
            start_ts + i * HOUR,
            "100.0",
            "100.5",
            "99.5",
            "100.0",
            "1000.0",
            start_ts + i * HOUR + HOUR - 1,
            "0",
            0,
            "0",
            "0",
            "0",
        ]
        for i in range(count)
    ]


def make_state(last_processed):
    return {
        "equity": 1000.0,
        "peak_equity": 1000.0,
        "position": None,
        "pending_entry": None,
        "pending_events": [],
        "halt_trading": False,
        "last_processed_candle_ts": last_processed,
        "symbol": "BTC-SWAP-USDT",
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "circuit_breaker_active": False,
    }


def read_status(state_dir):
    path = os.path.join(state_dir, "paper_status.json")
    assert os.path.exists(path), "paper_status.json was not created"

    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def run_with_mocks(
    monkeypatch,
    state_dir,
    state,
    klines,
    *,
    process_new_candles=False,
    notify_mock=None,
    status_writer=None,
    load_state_mock=None,
    reconciler_mock=None,
):
    monkeypatch.setenv("STATE_DIR", state_dir)
    monkeypatch.setenv("SYMBOL", "BTC-SWAP-USDT")
    monkeypatch.setenv("INTERVAL", "1h")
    monkeypatch.setenv("APEX_SAFE_MODE", "1")

    monkeypatch.setattr(
        rm,
        "compute_strategy_hash",
        lambda: FAKE_HASH,
    )
    monkeypatch.setattr(
        rm,
        "state_exists",
        lambda _: True,
    )

    if load_state_mock is None:
        load_state_mock = lambda _: copy.deepcopy(state)

    monkeypatch.setattr(
        rm,
        "load_state",
        load_state_mock,
    )
    monkeypatch.setattr(
        rm,
        "save_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        rm,
        "append_events",
        lambda *_args, **_kwargs: (0, 0),
    )
    monkeypatch.setattr(
        rm,
        "get_symbol_specs",
        lambda _: FAKE_SPECS,
    )
    monkeypatch.setattr(
        rm,
        "fetch_closed_klines",
        lambda *_args, **_kwargs: klines,
    )
    monkeypatch.setattr(
        rm,
        "_build_indicators",
        lambda _: {"test_indicators": True},
    )

    if reconciler_mock is None:
        reconciler_mock = lambda *_args, **_kwargs: {
            "status": "OK",
            "summary": "test reconciliation passed",
        }

    monkeypatch.setattr(
        rm,
        "reconcile_with_data",
        reconciler_mock,
    )

    monkeypatch.setattr(
        rm,
        "_build_engine_config_with_kill_switch",
        lambda *_args, **_kwargs: {
            "leverage": 1,
            "kill_switch_active": False,
            "kill_switch_reason": None,
        },
    )

    class FakeExecutionManager:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, *args, **kwargs):
            raise AssertionError(
                "Execution must not be called by status tests."
            )

    monkeypatch.setattr(
        rm,
        "ExecutionManager",
        FakeExecutionManager,
    )

    if process_new_candles:
        monkeypatch.setattr(
            rm,
            "evaluate_signal",
            lambda *_args, **_kwargs: {
                "signal": "NO_TRADE",
            },
        )

        def fake_process_candle(current_state, *_args, **_kwargs):
            updated = copy.deepcopy(current_state)
            updated["equity"] = 1001.0
            updated["peak_equity"] = 1001.0
            return updated, []

        monkeypatch.setattr(
            rm,
            "process_candle",
            fake_process_candle,
        )

    if notify_mock is None:
        notify_mock = lambda _status: True

    monkeypatch.setattr(
        rm,
        "notify_run_status",
        notify_mock,
    )

    if status_writer is not None:
        monkeypatch.setattr(
            rm,
            "_write_paper_status_atomic",
            status_writer,
        )

    return rm.main()


def test_atomic_status_writer_creates_valid_json():
    with tempfile.TemporaryDirectory() as td:
        expected = {
            "schema_version": "1.0.0",
            "run_status": "success",
        }

        rm._write_paper_status_atomic(td, expected)

        assert read_status(td) == expected


def test_atomic_status_writer_creates_directory():
    with tempfile.TemporaryDirectory() as td:
        nested = os.path.join(td, "a", "b")

        rm._write_paper_status_atomic(
            nested,
            {"run_status": "success"},
        )

        assert os.path.isfile(
            os.path.join(nested, "paper_status.json")
        )


def test_no_op_writes_complete_status(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[-1][0]))

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
        )

        status = read_status(td)

        assert exit_code == 0
        assert status["run_status"] == "no_op"
        assert status["run_exit_code"] == 0
        assert status["candles_processed_this_run"] == 0
        assert status["equity"] == 1000.0
        assert status["peak_equity"] == 1000.0
        assert status["has_position"] is False
        assert status["reconciler_status"] == "OK"


def test_status_contains_all_required_fields(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[-1][0]))

        run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
        )

        status = read_status(td)
        missing = REQUIRED_FIELDS - set(status)

        assert not missing, f"Missing fields: {sorted(missing)}"


def test_bootstrap_writes_latest_candle_timestamp(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(None)

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
        )

        status = read_status(td)

        assert exit_code == 0
        assert status["run_status"] == "bootstrap"
        assert status["last_processed_candle_ts"] == int(
            klines[-1][0]
        )
        assert status["candles_processed_this_run"] == 0


def test_bootstrap_rejects_existing_position(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(None)
        state["position"] = {
            "side": "LONG",
            "entry_price": 100.0,
        }

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
        )

        status = read_status(td)

        assert exit_code == 1
        assert status["run_status"] == "aborted"
        assert status["error_message"] == "bootstrap_conflict"


def test_insufficient_data_does_not_call_reconciler(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(200)
        state = make_state(None)

        def reconciler_must_not_run(*_args, **_kwargs):
            raise AssertionError(
                "Reconciler must not run with insufficient candles."
            )

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            reconciler_mock=reconciler_must_not_run,
        )

        status = read_status(td)

        assert exit_code == 0
        assert status["run_status"] == "insufficient_data"
        assert status["reconciler_status"] is None
        assert status["candles_processed_this_run"] == 0


def test_reconciler_error_aborts_run(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[298][0]))

        def reconciler_error(*_args, **_kwargs):
            return {
                "status": "ERROR",
                "summary": "test reconciliation error",
            }

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            reconciler_mock=reconciler_error,
        )

        status = read_status(td)

        assert exit_code == 1
        assert status["run_status"] == "aborted"
        assert status["reconciler_status"] == "ERROR"
        assert "reconciler" in status["error_message"]


def test_success_updates_candle_timestamp_and_equity(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[298][0]))

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            process_new_candles=True,
        )

        status = read_status(td)

        assert exit_code == 0
        assert status["run_status"] == "success"
        assert status["candles_processed_this_run"] == 1
        assert status["total_new_detected"] == 1
        assert status["last_processed_candle_ts"] == int(
            klines[299][0]
        )
        assert status["equity"] == 1001.0
        assert status["peak_equity"] == 1001.0


def test_partial_catchup_is_reported(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[197][0]))

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            process_new_candles=True,
        )

        status = read_status(td)

        assert exit_code == 0
        assert status["run_status"] == "partial_catchup"
        assert status["total_new_detected"] == 102
        assert status["candles_processed_this_run"] == 100
        assert status["catchup_lag"] == 2
        assert status["last_processed_candle_ts"] == int(
            klines[297][0]
        )


def test_notifier_failure_does_not_change_run_result(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[-1][0]))

        def failing_notifier(_status):
            raise RuntimeError("telegram unavailable")

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            notify_mock=failing_notifier,
        )

        status = read_status(td)

        assert exit_code == 0
        assert status["run_status"] == "no_op"
        assert status["run_exit_code"] == 0


def test_status_write_failure_still_notifies(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[-1][0]))
        notifications = []

        def failing_writer(*_args, **_kwargs):
            raise OSError("disk full")

        def capture_notification(status):
            notifications.append(copy.deepcopy(status))
            return True

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            notify_mock=capture_notification,
            status_writer=failing_writer,
        )

        assert exit_code == 1
        assert len(notifications) == 1
        assert notifications[0]["run_exit_code"] == 1
        assert notifications[0]["run_status"] == "no_op"
        assert "paper_status_write" in notifications[0]["error_message"]


def test_state_load_validation_error_is_aborted(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        klines = make_candles(300)
        state = make_state(int(klines[-1][0]))

        def invalid_state_loader(_state_dir):
            raise ValueError("strategy hash mismatch")

        exit_code = run_with_mocks(
            monkeypatch,
            td,
            state,
            klines,
            load_state_mock=invalid_state_loader,
        )

        status = read_status(td)

        assert exit_code == 1
        assert status["run_status"] == "aborted"
        assert "strategy hash mismatch" in status["error_message"]