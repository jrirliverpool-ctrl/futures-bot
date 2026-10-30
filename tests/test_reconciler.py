import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from reconciler import reconcile_with_data
from compute_hash import compute_strategy_hash


def _state():
    return {
        "initial_equity": 1000.0,
        "equity": 1000.0,
        "peak_equity": 1000.0,
        "strategy_hash": compute_strategy_hash(),
        "circuit_breaker_active": False,
        "halt_trading": False,
        "order_status": None,
        "last_processed_candle_ts": 0,
    }


def _klines(n=300):
    return [
        [
            1790000000000 + i * 3600000,
            "100",
            "101",
            "99",
            "100",
            "10",
        ]
        for i in range(n)
    ]


def test_clean_state_ok():
    state = _state()
    klines = _klines()

    state["last_processed_candle_ts"] = klines[-1][0]

    result = reconcile_with_data(
        state,
        klines,
        "1h",
        klines[-1][0] + 3600000,
    )

    assert result["status"] == "OK"


def test_missing_hash_error():
    state = _state()
    del state["strategy_hash"]

    klines = _klines()
    state["last_processed_candle_ts"] = klines[-1][0]

    result = reconcile_with_data(
        state,
        klines,
        "1h",
        klines[-1][0] + 60000,
    )

    assert result["status"] == "ERROR"


def test_corrupt_ohlc_error():
    state = _state()
    klines = _klines()

    klines[10] = [
        klines[10][0],
        "invalid",
        "101",
        "99",
        "100",
        "10",
    ]

    state["last_processed_candle_ts"] = klines[-1][0]

    result = reconcile_with_data(
        state,
        klines,
        "1h",
        klines[-1][0] + 60000,
    )

    assert result["status"] == "ERROR"


def test_ohlc_logical_violation_error():
    state = _state()
    klines = _klines()

    # high < low
    klines[10] = [
        klines[10][0],
        "100",
        "90",
        "110",
        "105",
        "10",
    ]

    state["last_processed_candle_ts"] = klines[-1][0]

    result = reconcile_with_data(
        state,
        klines,
        "1h",
        klines[-1][0] + 60000,
    )

    assert result["status"] == "ERROR"


def test_stale_data_warning():
    state = _state()
    klines = _klines()

    state["last_processed_candle_ts"] = klines[-1][0]

    result = reconcile_with_data(
        state,
        klines,
        "1h",
        klines[-1][0] + 10 * 3600000,
    )

    assert result["status"] == "WARNING"


def test_unknown_interval_error():
    state = _state()
    klines = _klines()

    state["last_processed_candle_ts"] = klines[-1][0]

    result = reconcile_with_data(
        state,
        klines,
        "invalid_tf",
        klines[-1][0] + 60000,
    )

    assert result["status"] == "ERROR"