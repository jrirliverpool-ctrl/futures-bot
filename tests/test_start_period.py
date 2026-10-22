"""
test_start_period.py — Verifies safe archival and reset.
"""
import os
import sys
import json
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

@pytest.fixture
def mock_state_dir(tmp_path):
    state_dir = str(tmp_path)
    state = {
        "equity": 971.83,
        "initial_equity": 1000.0,
        "strategy_hash": "hash123",
        "last_processed_candle_ts": 123456789,
        "position": {"side": "LONG"}
    }
    with open(os.path.join(state_dir, "state.json"), "w") as f:
        json.dump(state, f)
    with open(os.path.join(state_dir, "events.jsonl"), "w") as f:
        f.write('{"event_id": "old"}\n')
    return state_dir

def test_start_new_period(mock_state_dir):
    with patch.dict(os.environ, {"STATE_DIR": mock_state_dir}):
        import start_new_period
        start_new_period.main()

    # 1. بررسی آرشیو
    archive_path = os.path.join(mock_state_dir, "archive_v1")
    assert os.path.exists(os.path.join(archive_path, "state.json"))
    assert os.path.exists(os.path.join(archive_path, "events.jsonl"))

    # 2. بررسی State جدید
    with open(os.path.join(mock_state_dir, "state.json")) as f:
        new_state = json.load(f)
    
    assert new_state["initial_equity"] == 971.83
    assert new_state["equity"] == 971.83
    assert new_state["position"] is None
    assert new_state["last_processed_candle_ts"] == 123456789
    assert "period_start_utc" in new_state

    # 3. بررسی خالی بودن Events
    with open(os.path.join(mock_state_dir, "events.jsonl")) as f:
        assert f.read() == ""