
import os
import sys
import json

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

from compute_hash import get_strategy_metadata
from start_new_period import start_new_period


def create_state_dir(tmp_path):
    metadata = get_strategy_metadata()

    state = {
        "version": "1.0.0",
        "strategy_version": metadata["version"],
        "strategy_hash": metadata["hash"],
        "symbol": "BTC-SWAP-USDT",
        "initial_equity": 1000.0,
        "equity": 971.83,
        "peak_equity": 1020.0,
        "position": None,
        "pending_entry": None,
        "pending_events": [],
        "last_processed_candle_ts": 123456789,
        "daily_loss": 3.0,
        "weekly_loss": 8.0,
        "loss_day_key": 20000,
        "loss_week_key": 2800,
        "circuit_breaker_active": False,
        "halt_trading": False,
        "last_funding_ts": None,
    }

    state_path = tmp_path / "state.json"
    events_path = tmp_path / "events.jsonl"

    state_path.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )

    events = [
        {
            "event_id": "event-1",
            "seq": 1,
            "event_type": "COOLDOWN_SKIP",
        },
        {
            "event_id": "event-2",
            "seq": 2,
            "event_type": "SIGNAL",
        },
    ]

    events_path.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )

    return state, state_path, events_path


def read_json(path):
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def test_period_start_preserves_risk_and_equity(tmp_path):
    original, state_path, events_path = create_state_dir(tmp_path)

    original_events = events_path.read_bytes()

    result = start_new_period(str(tmp_path))

    new_state = read_json(state_path)

    assert new_state["initial_equity"] == original["initial_equity"]
    assert new_state["equity"] == original["equity"]
    assert new_state["peak_equity"] == original["peak_equity"]

    assert new_state["daily_loss"] == original["daily_loss"]
    assert new_state["weekly_loss"] == original["weekly_loss"]

    assert new_state["loss_day_key"] == original["loss_day_key"]
    assert new_state["loss_week_key"] == original["loss_week_key"]

    assert (
        new_state["circuit_breaker_active"]
        == original["circuit_breaker_active"]
    )

    assert (
        new_state["last_processed_candle_ts"]
        == original["last_processed_candle_ts"]
    )

    assert new_state["strategy_version"] == original["strategy_version"]
    assert new_state["strategy_hash"] == original["strategy_hash"]

    assert new_state["period_start_equity"] == original["equity"]
    assert new_state["period_id"] == result["period_id"]

    assert events_path.read_bytes() == b""

    archive_dir = tmp_path / "archive_v1" / result["period_id"]

    assert (archive_dir / "state.json").read_bytes() == (
        json.dumps(
            original,
            indent=2,
        ).encode("utf-8")
    ) or read_json(archive_dir / "state.json") == original

    assert (
        archive_dir / "events.jsonl"
    ).read_bytes() == original_events

    manifest = read_json(
        archive_dir / "archive_manifest.json"
    )

    assert manifest["archive_status"] == "complete"
    assert manifest["source"]["events_count"] == 2
    assert manifest["risk_state_preserved"] is True


def test_open_position_blocks_period_start(tmp_path):
    state, state_path, events_path = create_state_dir(tmp_path)

    state["position"] = {
        "side": "LONG",
        "qty": 1.0,
    }

    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    original_state = state_path.read_bytes()
    original_events = events_path.read_bytes()

    with pytest.raises(RuntimeError, match="Open position"):
        start_new_period(str(tmp_path))

    assert state_path.read_bytes() == original_state
    assert events_path.read_bytes() == original_events


def test_pending_entry_blocks_period_start(tmp_path):
    state, state_path, _ = create_state_dir(tmp_path)

    state["pending_entry"] = {
        "side": "SHORT",
    }

    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Pending entry"):
        start_new_period(str(tmp_path))


def test_pending_events_block_period_start(tmp_path):
    state, state_path, _ = create_state_dir(tmp_path)

    state["pending_events"] = [
        {"event_type": "ENTRY"}
    ]

    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Pending events"):
        start_new_period(str(tmp_path))


def test_strategy_hash_mismatch_blocks_period_start(tmp_path):
    state, state_path, _ = create_state_dir(tmp_path)

    state["strategy_hash"] = "sha256:invalid"

    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Strategy hash mismatch"):
        start_new_period(str(tmp_path))


def test_strategy_version_mismatch_blocks_period_start(tmp_path):
    state, state_path, _ = create_state_dir(tmp_path)

    state["strategy_version"] = "1.0.1"

    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Strategy version mismatch"):
        start_new_period(str(tmp_path))


def test_archive_is_never_overwritten(tmp_path):
    _, state_path, _ = create_state_dir(tmp_path)

    first = start_new_period(str(tmp_path))

    archive_dir = (
        tmp_path
        / "archive_v1"
        / first["period_id"]
    )

    original_archive = (
        archive_dir / "state.json"
    ).read_bytes()

    # The second period archives the first period's current state.
    second = start_new_period(str(tmp_path))

    assert first["period_id"] != second["period_id"]

    assert (
        archive_dir / "state.json"
    ).read_bytes() == original_archive

    assert (
        tmp_path
        / "archive_v1"
        / second["period_id"]
        / "archive_manifest.json"
    ).exists()


def test_corrupt_event_log_blocks_period_start(tmp_path):
    _, _, events_path = create_state_dir(tmp_path)

    events_path.write_text(
        '{"event_id": "valid"}\nnot-json\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Invalid JSON"):
        start_new_period(str(tmp_path))


def test_risk_state_is_not_reset_when_circuit_breaker_active(tmp_path):
    state, state_path, _ = create_state_dir(tmp_path)

    state["circuit_breaker_active"] = True
    state["halt_trading"] = True

    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    start_new_period(str(tmp_path))

    updated = read_json(state_path)

    assert updated["circuit_breaker_active"] is True
    assert updated["halt_trading"] is True
