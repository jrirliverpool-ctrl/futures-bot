"""
test_paper_state_manager.py

Tests for scripts/paper_state_manager.py.

Coverage:
- state_exists
- load_events
- append_events
- events_count
- max_seq

Scope:
- Tests only the native behavior of paper_state_manager.py.
- Does not test delegated init_state/load_state/save_state behavior.
- Does not assume atomic JSONL writes.
"""

import json
import os
import sys
import tempfile

import pytest


SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")
)

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


from paper_state_manager import (
    state_exists,
    load_events,
    append_events,
    events_count,
    max_seq,
    events_file,
    state_file,
)


# ============================================================
# Helpers
# ============================================================

def _write_events_file(state_dir, content):
    os.makedirs(state_dir, exist_ok=True)

    path = events_file(state_dir)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path


def _event(event_type="SIGNAL", timestamp=1000, **extra):
    event = {
        "type": event_type,
        "timestamp": timestamp,
    }
    event.update(extra)
    return event


# ============================================================
# state_exists
# ============================================================

def test_state_exists_false_when_state_file_missing():
    with tempfile.TemporaryDirectory() as state_dir:
        assert state_exists(state_dir) is False


def test_state_exists_true_when_state_file_exists():
    with tempfile.TemporaryDirectory() as state_dir:
        os.makedirs(state_dir, exist_ok=True)

        path = state_file(state_dir)

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"equity": 1000.0}, f)

        assert state_exists(state_dir) is True


def test_state_exists_does_not_confuse_events_file_with_state_file():
    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1, "event_type": "SIGNAL"}\n',
        )

        assert state_exists(state_dir) is False


# ============================================================
# load_events
# ============================================================

def test_load_events_returns_empty_list_when_file_missing():
    with tempfile.TemporaryDirectory() as state_dir:
        assert load_events(state_dir) == []


def test_load_events_reads_valid_jsonl():
    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1, "event_type": "SIGNAL"}\n'
            '{"seq": 2, "event_type": "ENTRY"}\n',
        )

        loaded = load_events(state_dir)

        assert len(loaded) == 2
        assert loaded[0]["seq"] == 1
        assert loaded[1]["seq"] == 2


def test_load_events_skips_blank_lines():
    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1}\n'
            '\n'
            '   \n'
            '{"seq": 2}\n',
        )

        loaded = load_events(state_dir)

        assert len(loaded) == 2
        assert loaded[0]["seq"] == 1
        assert loaded[1]["seq"] == 2


def test_load_events_raises_on_corrupt_json_line():
    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1}\n'
            'this is not json\n',
        )

        with pytest.raises(ValueError, match="line 2"):
            load_events(state_dir)


def test_load_events_raises_when_last_line_is_incomplete():
    """
    The implementation is append-only, not atomic.
    A partial final JSONL line must fail closed.
    """

    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1}\n'
            '{"seq": 2',
        )

        with pytest.raises(ValueError):
            load_events(state_dir)


# ============================================================
# append_events — empty input
# ============================================================

def test_append_events_empty_list_returns_zero_counts():
    with tempfile.TemporaryDirectory() as state_dir:
        appended, skipped = append_events(state_dir, [])

        assert appended == 0
        assert skipped == 0


def test_append_events_empty_list_does_not_create_events_file():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(state_dir, [])

        assert os.path.exists(events_file(state_dir)) is False


# ============================================================
# append_events — sequence assignment
# ============================================================

def test_append_events_assigns_sequential_seq_numbers():
    with tempfile.TemporaryDirectory() as state_dir:
        events = [
            _event("SIGNAL", 1000),
            _event("ENTRY", 2000),
        ]

        appended, skipped = append_events(
            state_dir,
            events,
            strategy_hash="test-hash",
        )

        assert appended == 2
        assert skipped == 0

        loaded = load_events(state_dir)

        assert len(loaded) == 2
        assert loaded[0]["seq"] == 1
        assert loaded[1]["seq"] == 2


def test_append_events_continues_sequence_across_calls():
    with tempfile.TemporaryDirectory() as state_dir:
        first_appended, first_skipped = append_events(
            state_dir,
            [_event("SIGNAL", 1000)],
            strategy_hash="test-hash",
        )

        second_appended, second_skipped = append_events(
            state_dir,
            [_event("ENTRY", 2000)],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)

        assert first_appended == 1
        assert first_skipped == 0

        assert second_appended == 1
        assert second_skipped == 0

        assert [event["seq"] for event in loaded] == [1, 2]


def test_append_events_continues_after_existing_sequence_gap():
    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 2, "event_id": "existing-1"}\n'
            '{"seq": 7, "event_id": "existing-2"}\n',
        )

        appended, skipped = append_events(
            state_dir,
            [_event("ENTRY", 3000)],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)

        assert appended == 1
        assert skipped == 0
        assert loaded[-1]["seq"] == 8


# ============================================================
# append_events — field transformation
# ============================================================

def test_append_events_renames_type_to_event_type():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [_event("SIGNAL", 1000)],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)
        event = loaded[0]

        assert "type" not in event
        assert event["event_type"] == "SIGNAL"


def test_append_events_transforms_timestamp():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [_event("SIGNAL", 1000)],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)
        event = loaded[0]

        assert "timestamp" not in event
        assert event["candle_ts_ms"] == 1000
        assert "timestamp_iso" in event
        assert event["timestamp_iso"].endswith("+00:00")


def test_append_events_preserves_other_event_fields():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [
                _event(
                    "ENTRY",
                    1000,
                    side="LONG",
                    price=85000.0,
                    size=2.0,
                )
            ],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)
        event = loaded[0]

        assert event["side"] == "LONG"
        assert event["price"] == 85000.0
        assert event["size"] == 2.0


def test_append_events_adds_strategy_hash():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [_event("SIGNAL", 1000)],
            strategy_hash="custom-strategy-hash",
        )

        loaded = load_events(state_dir)

        assert loaded[0]["strategy_hash"] == "custom-strategy-hash"


def test_append_events_adds_strategy_version():
    from strategy_config import STRATEGY_CONFIG

    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [_event("SIGNAL", 1000)],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)

        assert loaded[0]["strategy_version"] == STRATEGY_CONFIG["version"]


def test_append_events_adds_event_id():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [_event("SIGNAL", 1000)],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)

        assert "event_id" in loaded[0]
        assert isinstance(loaded[0]["event_id"], str)
        assert len(loaded[0]["event_id"]) == 16


# ============================================================
# append_events — idempotency
# ============================================================

def test_append_events_skips_duplicate_event():
    with tempfile.TemporaryDirectory() as state_dir:
        event = _event("SIGNAL", 1000, signal="LONG")

        first_appended, first_skipped = append_events(
            state_dir,
            [event],
            strategy_hash="test-hash",
        )

        second_appended, second_skipped = append_events(
            state_dir,
            [event],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)

        assert first_appended == 1
        assert first_skipped == 0

        assert second_appended == 0
        assert second_skipped == 1

        assert len(loaded) == 1


def test_append_events_does_not_skip_different_events():
    with tempfile.TemporaryDirectory() as state_dir:
        first = _event("SIGNAL", 1000, signal="LONG")
        second = _event("SIGNAL", 2000, signal="SHORT")

        appended, skipped = append_events(
            state_dir,
            [first, second],
            strategy_hash="test-hash",
        )

        loaded = load_events(state_dir)

        assert appended == 2
        assert skipped == 0
        assert len(loaded) == 2


def test_append_events_fingerprint_depends_on_strategy_hash():
    with tempfile.TemporaryDirectory() as state_dir:
        event = _event("SIGNAL", 1000, signal="LONG")

        first_appended, first_skipped = append_events(
            state_dir,
            [event],
            strategy_hash="hash-A",
        )

        second_appended, second_skipped = append_events(
            state_dir,
            [event],
            strategy_hash="hash-B",
        )

        loaded = load_events(state_dir)

        assert first_appended == 1
        assert first_skipped == 0

        assert second_appended == 1
        assert second_skipped == 0

        assert len(loaded) == 2
        assert loaded[0]["strategy_hash"] == "hash-A"
        assert loaded[1]["strategy_hash"] == "hash-B"


# ============================================================
# append_events — corrupted existing file
# ============================================================

def test_append_events_refuses_to_append_to_corrupt_file():
    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1}\n'
            'corrupt-json-line\n',
        )

        with pytest.raises(ValueError, match="line 2"):
            append_events(
                state_dir,
                [_event("ENTRY", 2000)],
                strategy_hash="test-hash",
            )


def test_append_events_does_not_silently_repair_corrupt_file():
    with tempfile.TemporaryDirectory() as state_dir:
        original_content = (
            '{"seq": 1}\n'
            'corrupt-json-line\n'
        )

        path = _write_events_file(state_dir, original_content)

        with pytest.raises(ValueError):
            append_events(
                state_dir,
                [_event("ENTRY", 2000)],
                strategy_hash="test-hash",
            )

        with open(path, "r", encoding="utf-8") as f:
            after_content = f.read()

        assert after_content == original_content


# ============================================================
# events_count
# ============================================================

def test_events_count_returns_zero_when_file_missing():
    with tempfile.TemporaryDirectory() as state_dir:
        assert events_count(state_dir) == 0


def test_events_count_counts_physical_lines():
    """
    Current implementation counts physical lines,
    including blank lines.
    """

    with tempfile.TemporaryDirectory() as state_dir:
        _write_events_file(
            state_dir,
            '{"seq": 1}\n'
            '\n'
            '{"seq": 2}\n',
        )

        assert events_count(state_dir) == 3


def test_events_count_after_append():
    with tempfile.TemporaryDirectory() as state_dir:
        append_events(
            state_dir,
            [
                _event("SIGNAL", 1000),
                _event("ENTRY", 2000),
            ],
            strategy_hash="test-hash",
        )

        assert events_count(state_dir) == 2


# ============================================================
# max_seq
# ============================================================

def test_max_seq_returns_zero_for_empty_list():
    assert max_seq([]) == 0


def test_max_seq_returns_largest_sequence():
    events = [
        {"seq": 1},
        {"seq": 5},
        {"seq": 3},
    ]

    assert max_seq(events) == 5


def test_max_seq_defaults_missing_seq_to_zero():
    events = [
        {},
        {"seq": 3},
        {"seq": 1},
    ]

    assert max_seq(events) == 3


def test_max_seq_accepts_zero():
    assert max_seq([{"seq": 0}]) == 0


def test_max_seq_rejects_string_sequence():
    with pytest.raises(ValueError, match="must be int"):
        max_seq([{"seq": "3"}])


def test_max_seq_rejects_float_sequence():
    with pytest.raises(ValueError, match="must be int"):
        max_seq([{"seq": 3.0}])


def test_max_seq_rejects_boolean_sequence():
    """
    bool is a subclass of int in Python.
    The implementation explicitly rejects bool.
    """

    with pytest.raises(ValueError, match="must be int"):
        max_seq([{"seq": True}])


def test_max_seq_rejects_negative_sequence():
    with pytest.raises(ValueError, match="must be >= 0"):
        max_seq([{"seq": -1}])


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    raise SystemExit(
        "Run this file with pytest; no standalone runner is defined."
    )