
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import live_execution_state as state_module

from live_execution_state import (
    BALANCE_SOURCE,
    POSITION_SOURCE,
    LiveExecutionStateStore,
    derive_positions_from_fills,
)


ACCOUNT = "toobit-usdtm-test-account"
STRATEGY_HASH = "sha256:test-strategy-hash"
SYMBOL = "BTC-SWAP-USDT"
NOW = 2_000_000


def fill(trade_id, side, qty, price, timestamp):
    return {
        "trade_id": trade_id,
        "symbol": SYMBOL,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "timestamp_ms": timestamp,
    }


def make_store(tmp_path):
    return LiveExecutionStateStore(
        path=str(tmp_path / "private" / "live_state.json"),
        account_identity=ACCOUNT,
    )


def ready_store(tmp_path, fills):
    store = make_store(tmp_path)

    store.initialize(
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=NOW - 1000,
    )

    store.record_fills(
        fills,
        now_ms=NOW - 100,
    )

    store.record_balance_snapshot(
        {
            "source": BALANCE_SOURCE,
            "asset": "USDT",
            "wallet_balance": "100",
            "available_balance": "80",
            "exchange_timestamp_ms": NOW,
        },
        now_ms=NOW,
    )

    return store


def reconciliation(exchange_positions):
    return {
        "source": POSITION_SOURCE,
        "ok": True,
        "halt_trading": False,
        "reasons": [],
        "symbols_expected": [SYMBOL],
        "symbols_checked": [SYMBOL],
        "exchange_positions": exchange_positions,
        "exchange_timestamp_ms": NOW,
    }


def test_opposite_open_creates_independent_hedge_positions():
    result = derive_positions_from_fills([
        fill("long", "BUY_OPEN", "2", "100", NOW - 200),
        fill("short", "SELL_OPEN", "1", "110", NOW - 100),
    ])

    assert set(result[SYMBOL]) == {"LONG", "SHORT"}
    assert result[SYMBOL]["LONG"]["quantity"] == "2"
    assert result[SYMBOL]["SHORT"]["quantity"] == "1"


def test_closing_long_preserves_short_position():
    result = derive_positions_from_fills([
        fill("long", "BUY_OPEN", "2", "100", NOW - 300),
        fill("short", "SELL_OPEN", "1", "110", NOW - 200),
        fill("close-long", "SELL_CLOSE", "1", "105", NOW - 100),
    ])

    assert set(result[SYMBOL]) == {"LONG", "SHORT"}
    assert result[SYMBOL]["LONG"]["quantity"] == "1"
    assert result[SYMBOL]["SHORT"]["quantity"] == "1"


def test_closing_short_preserves_long_position():
    result = derive_positions_from_fills([
        fill("long", "BUY_OPEN", "2", "100", NOW - 300),
        fill("short", "SELL_OPEN", "1", "110", NOW - 200),
        fill("close-short", "BUY_CLOSE", "1", "105", NOW - 100),
    ])

    # One remaining side must use the legacy-compatible representation.
    assert result[SYMBOL] == {
        "side": "LONG",
        "quantity": "2",
        "avg_price": "100",
    }


def test_closing_long_preserves_short_in_legacy_format():
    result = derive_positions_from_fills([
        fill("long", "BUY_OPEN", "2", "100", NOW - 300),
        fill("short", "SELL_OPEN", "1", "110", NOW - 200),
        fill("close-long", "SELL_CLOSE", "2", "105", NOW - 100),
    ])

    assert result[SYMBOL] == {
        "side": "SHORT",
        "quantity": "1",
        "avg_price": "110",
    }


def test_single_side_result_remains_backward_compatible():
    result = derive_positions_from_fills([
        fill("long", "BUY_OPEN", "2", "100", NOW - 100),
    ])

    assert result[SYMBOL] == {
        "side": "LONG",
        "quantity": "2",
        "avg_price": "100",
    }


def test_hedge_positions_survive_store_reload(tmp_path):
    fills = [
        fill("long", "BUY_OPEN", "2", "100", NOW - 300),
        fill("short", "SELL_OPEN", "1", "110", NOW - 200),
        fill("close-long", "SELL_CLOSE", "0.5", "105", NOW - 100),
    ]

    store = make_store(tmp_path)

    store.initialize(
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=NOW - 1000,
    )

    result = store.record_fills(fills, now_ms=NOW - 100)
    reloaded = make_store(tmp_path).load()

    assert result["positions"] == reloaded["positions"]
    assert reloaded["positions"][SYMBOL]["LONG"]["quantity"] == "1.5"
    assert reloaded["positions"][SYMBOL]["SHORT"]["quantity"] == "1"


def test_reconciliation_accepts_both_hedge_sides(tmp_path):
    fills = [
        fill("long", "BUY_OPEN", "2", "100", NOW - 300),
        fill("short", "SELL_OPEN", "1", "110", NOW - 200),
    ]

    store = ready_store(tmp_path, fills)

    result = store.record_reconciliation(
        reconciliation({
            SYMBOL: {
                "LONG": {
                    "quantity": "2",
                    "avg_price": "100",
                },
                "SHORT": {
                    "quantity": "1",
                    "avg_price": "110",
                },
            }
        }),
        now_ms=NOW,
    )

    assert result["halted"] is False
    assert set(result["positions"][SYMBOL]) == {"LONG", "SHORT"}


def test_windows_lock_fallback_uses_one_byte_lock(tmp_path, monkeypatch):
    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1

        @staticmethod
        def locking(fd, mode, count):
            calls.append((fd, mode, count))

    lock_path = tmp_path / "windows-test.lock"

    monkeypatch.setattr(state_module, "fcntl", None)
    monkeypatch.setattr(
        state_module,
        "msvcrt",
        FakeMsvcrt,
        raising=False,
    )

    with open(lock_path, "a+b") as handle:
        state_module._lock_exclusive(handle)

    assert calls
    assert calls[0][1:] == (FakeMsvcrt.LK_LOCK, 1)

    assert lock_path.read_bytes() == b"\0"
