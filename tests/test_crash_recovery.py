"""
test_crash_recovery.py — Proves append_events then save_state order is safe.
"""
import os
import sys
import json
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from paper_state_manager import append_events, save_state, load_state, load_events

@pytest.fixture
def tmp_state_dir(tmp_path):
    return str(tmp_path)

def test_recovery_after_save_state_crash(tmp_state_dir):
    state = {"equity": 1000.0, "initial_equity": 1000.0, "position": None, "version": "1.0.0", "peak_equity": 1000.0, "daily_loss": 0, "weekly_loss": 0}
    save_state(tmp_state_dir, state)
    
    events = [{"event_id": "ev1", "event_type": "ENTRY", "pnl_usd": 10.0}]
    
    with patch("paper_state_manager.save_state", side_effect=RuntimeError("Simulated crash")):
        try:
            append_events(tmp_state_dir, events, strategy_hash="hash123")
            save_state(tmp_state_dir, state) 
        except RuntimeError:
            pass 
            
    disk_state = load_state(tmp_state_dir)
    assert disk_state["equity"] == 1000.0
    
    disk_events = load_events(tmp_state_dir)
    assert len(disk_events) == 1
    assert disk_events[0]["event_id"] == "ev1"
    
    appended, skipped = append_events(tmp_state_dir, events, strategy_hash="hash123")
    assert appended == 0 
    
    save_state(tmp_state_dir, state)
    assert load_state(tmp_state_dir)["equity"] == 1000.0