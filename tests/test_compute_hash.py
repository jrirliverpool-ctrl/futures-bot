import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from compute_hash import compute_strategy_hash, get_strategy_metadata

def test_hash_format():
    h = compute_strategy_hash()
    assert isinstance(h, str)
    assert h.startswith("sha256:")
    assert len(h) == 71

def test_hash_is_deterministic():
    assert compute_strategy_hash() == compute_strategy_hash()

def test_metadata_hash_matches_compute():
    assert get_strategy_metadata()["hash"] == compute_strategy_hash()