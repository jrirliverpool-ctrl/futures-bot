import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from reconciler import reconcile_with_data
from compute_hash import compute_strategy_hash

def _state():
    return {
        "initial_equity": 1000.0, "equity": 1000.0, "peak_equity": 1000.0,
        "strategy_hash": compute_strategy_hash(), "circuit_breaker_active": False,
        "halt_trading": False, "order_status": None,
        "last_processed_candle_ts": 0
    }

def _klines(n=300):
    return [[1790000000000 + i*3600000, "100", "101", "99", "100", "10"] for i in range(n)]

def test_clean_state_ok():
    s = _state()
    k = _klines()
    s["last_processed_candle_ts"] = k[-1][0]
    r = reconcile_with_data(s, k, "1h", k[-1][0] + 60000)
    assert r["status"] == "OK"

def test_missing_hash_error():
    s = _state()
    del s["strategy_hash"]
    k = _klines()
    s["last_processed_candle_ts"] = k[-1][0]
    r = reconcile_with_data(s, k, "1h", k[-1][0] + 60000)
    assert r["status"] == "ERROR"

def test_corrupt_ohlc_error():
    s = _state()
    k = _klines()
    k[10] = [k[10][0], "invalid", "101", "99", "100", "10"]
    s["last_processed_candle_ts"] = k[-1][0]
    r = reconcile_with_data(s, k, "1h", k[-1][0] + 60000)
    assert r["status"] == "ERROR"

def test_ohlc_logical_violation_error():
    s = _state()
    k = _klines()
    k[10] = [k[10][0], "100", "90", "110", "105", "10"]  # high < low
    s["last_processed_candle_ts"] = k[-1][0]
    r = reconcile_with_data(s, k, "1h", k[-1][0] + 60000)
    assert r["status"] == "ERROR"

def test_stale_data_warning():
    s = _state()
    k = _klines()
    s["last_processed_candle_ts"] = k[-1][0]
    r = reconcile_with_data(s, k, "1h", k[-1][0] + 10*3600000)
    assert r["status"] == "WARNING"

def test_unknown_interval_error():
    s = _state()
    k = _klines()
    s["last_processed_candle_ts"] = k[-1][0]
    r = reconcile_with_data(s, k, "invalid_tf", k[-1][0] + 60000)
    assert r["status"] == "ERROR"