
import os
import sys
import json
import hashlib

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from state_manager import default_state, save_state, load_state

from migrate_paper_state_v1_0_1_to_v1_1_0 import (
    migrate,
    SOURCE_HASH,
    TARGET_HASH,
    SOURCE_VERSION,
    TARGET_VERSION,
    EXPECTED_EVENT_COUNT,
    BACKUP_NAME,
    MANIFEST_NAME,
)


def create_fixture(directory):
    state = default_state(
        initial_equity=971.8365982771752
    )

    state.update({
        "strategy_hash": SOURCE_HASH,
        "strategy_version": SOURCE_VERSION,
        "symbol": "BTC-SWAP-USDT",
        "initial_equity": 971.8365982771752,
        "equity": 984.6741532799955,
        "peak_equity": 984.6741532799955,
        "last_processed_candle_ts": 1793376000000,
        "position": None,
        "pending_entry": None,
        "period_start_utc": "2026-10-22T01:34:38.853086+00:00",
        "loss_day_key": 20756,
        "loss_week_key": 2965,
    })

    save_state(str(directory), state)

    events_path = directory / "events.jsonl"

    with open(events_path, "w", encoding="utf-8") as f:
        for seq in range(1, EXPECTED_EVENT_COUNT + 1):
            event = {
                "event_type": "COOLDOWN_SKIP",
                "strategy_hash": SOURCE_HASH,
                "strategy_version": SOURCE_VERSION,
                "event_id": f"test-event-{seq}",
                "seq": seq,
                "until_index": 301,
            }
            f.write(json.dumps(event) + "\n")

    return state


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migration_preserves_state_and_events(tmp_path):
    original = create_fixture(tmp_path)

    state_path = tmp_path / "state.json"
    events_path = tmp_path / "events.jsonl"

    original_events_hash = sha256_file(events_path)

    migrate(str(tmp_path))

    migrated = load_state(str(state_path), strict=True)

    expected = dict(original)
    expected["strategy_hash"] = TARGET_HASH
    expected["strategy_version"] = TARGET_VERSION

    assert migrated == expected

    assert sha256_file(events_path) == original_events_hash

    backup_path = tmp_path / BACKUP_NAME
    assert backup_path.exists()

    backup = json.loads(backup_path.read_text(encoding="utf-8"))

    assert backup["strategy_hash"] == SOURCE_HASH
    assert backup["strategy_version"] == SOURCE_VERSION

    manifest_path = tmp_path / MANIFEST_NAME
    assert manifest_path.exists()

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )

    assert manifest["migration_status"] == "success"
    assert manifest["events_rewritten"] is False
    assert manifest["events_count"] == EXPECTED_EVENT_COUNT


def test_migration_refuses_open_position(tmp_path):
    create_fixture(tmp_path)

    state_path = tmp_path / "state.json"
    state = load_state(str(state_path), strict=False)

    state["position"] = {"side": "SHORT"}
    save_state(str(state_path), state)

    with pytest.raises(RuntimeError, match="Open position"):
        migrate(str(tmp_path))

    assert not (tmp_path / BACKUP_NAME).exists()


def test_migration_refuses_pending_entry(tmp_path):
    create_fixture(tmp_path)

    state_path = tmp_path / "state.json"
    state = load_state(str(state_path), strict=False)

    state["pending_entry"] = {"side": "LONG"}
    save_state(str(state_path), state)

    with pytest.raises(RuntimeError, match="Pending entry"):
        migrate(str(tmp_path))


def test_migration_refuses_unexpected_event_type(tmp_path):
    create_fixture(tmp_path)

    events_path = tmp_path / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()

    event = json.loads(lines[0])
    event["event_type"] = "ENTRY"

    lines[0] = json.dumps(event)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unexpected event type"):
        migrate(str(tmp_path))


def test_migration_refuses_unexpected_event_count(tmp_path):
    create_fixture(tmp_path)

    events_path = tmp_path / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()

    events_path.write_text(
        "\n".join(lines[:-1]) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Unexpected event count"):
        migrate(str(tmp_path))


def test_migration_refuses_second_execution(tmp_path):
    create_fixture(tmp_path)

    migrate(str(tmp_path))

    with pytest.raises(RuntimeError):
        migrate(str(tmp_path))
