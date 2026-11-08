import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from live_execution_state import (
    LiveExecutionStateStore,
    LiveStateError,
    create_initial_state,
    derive_positions_from_fills,
)


ACCOUNT = "toobit-usdtm-test-account"
STRATEGY_HASH = "sha256:test-strategy-hash"
SYMBOL = "BTC-SWAP-USDT"


def fill(
    trade_id,
    side,
    qty,
    price,
    timestamp_ms,
    symbol=SYMBOL,
):
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "timestamp_ms": timestamp_ms,
    }


def make_store(tmp_path):
    path = str(tmp_path / "private" / "live_state.json")

    return LiveExecutionStateStore(
        path=path,
        account_identity=ACCOUNT,
    )


def initialized_store(tmp_path):
    store = make_store(tmp_path)

    store.initialize(
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=1000,
    )

    return store


def test_initial_state_starts_halted():
    state = create_initial_state(
        account_identity=ACCOUNT,
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=1000,
    )

    assert state["halted"] is True
    assert (
        state["halt_reason"]
        == "initial_state_requires_reconciliation"
    )
    assert state["positions"] == {}
    assert state["processed_trade_ids"] == []
    assert state["sequence"] == 0


def test_invalid_environment_rejected():
    with pytest.raises(ValueError):
        create_initial_state(
            ACCOUNT,
            "PAPER",
            STRATEGY_HASH,
            now_ms=1000,
        )


def test_invalid_account_identity_rejected():
    with pytest.raises(LiveStateError):
        create_initial_state(
            "",
            "DEMO",
            STRATEGY_HASH,
            now_ms=1000,
        )


def test_long_position_weighted_average_and_partial_close():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", 1000),
        fill("2", "BUY_OPEN", "1", "120", 2000),
        fill("3", "SELL_CLOSE", "0.5", "110", 3000),
    ]

    positions = derive_positions_from_fills(fills)

    assert positions[SYMBOL]["side"] == "LONG"
    assert positions[SYMBOL]["quantity"] == "1.5"
    assert float(positions[SYMBOL]["avg_price"]) == pytest.approx(110)


def test_long_position_full_close_removes_position():
    fills = [
        fill("1", "BUY_OPEN", "2", "100", 1000),
        fill("2", "SELL_CLOSE", "2", "105", 2000),
    ]

    assert derive_positions_from_fills(fills) == {}


def test_short_position_and_partial_close():
    fills = [
        fill("1", "SELL_OPEN", "3", "100", 1000),
        fill("2", "BUY_CLOSE", "1", "90", 2000),
    ]

    positions = derive_positions_from_fills(fills)

    assert positions[SYMBOL]["side"] == "SHORT"
    assert positions[SYMBOL]["quantity"] == "2"
    assert positions[SYMBOL]["avg_price"] == "100"


def test_short_position_full_close_removes_position():
    fills = [
        fill("1", "SELL_OPEN", "2", "100", 1000),
        fill("2", "BUY_CLOSE", "2", "95", 2000),
    ]

    assert derive_positions_from_fills(fills) == {}


def test_multiple_symbols_are_independent():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", 1000, "BTC-SWAP-USDT"),
        fill("2", "SELL_OPEN", "2", "50", 1100, "ETH-SWAP-USDT"),
    ]

    positions = derive_positions_from_fills(fills)

    assert positions["BTC-SWAP-USDT"]["side"] == "LONG"
    assert positions["ETH-SWAP-USDT"]["side"] == "SHORT"


def test_duplicate_trade_id_rejected():
    fills = [
        fill("same-id", "BUY_OPEN", "1", "100", 1000),
        fill("same-id", "BUY_OPEN", "1", "100", 2000),
    ]

    with pytest.raises(LiveStateError, match="duplicate trade_id"):
        derive_positions_from_fills(fills)


def test_closing_fill_without_open_position_rejected():
    fills = [
        fill("1", "SELL_CLOSE", "1", "100", 1000),
    ]

    with pytest.raises(LiveStateError, match="no matching open"):
        derive_positions_from_fills(fills)


def test_closing_more_than_open_quantity_rejected():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", 1000),
        fill("2", "SELL_CLOSE", "2", "105", 2000),
    ]

    with pytest.raises(LiveStateError, match="exceeds position"):
        derive_positions_from_fills(fills)


def test_opposite_open_without_close_rejected():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", 1000),
        fill("2", "SELL_OPEN", "1", "105", 2000),
    ]

    with pytest.raises(LiveStateError, match="opposite-side open"):
        derive_positions_from_fills(fills)


@pytest.mark.parametrize(
    "bad_fill",
    [
        fill("1", "BAD_SIDE", "1", "100", 1000),
        fill("2", "BUY_OPEN", "0", "100", 1000),
        fill("3", "BUY_OPEN", "1", "0", 1000),
        fill("4", "BUY_OPEN", "NaN", "100", 1000),
        fill("5", "BUY_OPEN", "1", "Infinity", 1000),
        {
            "trade_id": "6",
            "symbol": SYMBOL,
            "side": "BUY_OPEN",
            "qty": "1",
            "price": "100",
            "timestamp_ms": True,
        },
    ],
)
def test_invalid_fill_rejected(bad_fill):
    with pytest.raises(LiveStateError):
        derive_positions_from_fills([bad_fill])


def test_store_initializes_and_loads(tmp_path):
    store = initialized_store(tmp_path)

    state = store.load()

    assert state["environment"] == "DEMO"
    assert state["account_identity"] == ACCOUNT
    assert state["sequence"] == 0
    assert state["halted"] is True


def test_missing_state_fails_closed(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(LiveStateError, match="missing"):
        store.load()


def test_initialize_refuses_to_overwrite_existing_state(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="already exists"):
        store.initialize(
            environment="DEMO",
            strategy_hash=STRATEGY_HASH,
            now_ms=2000,
        )


def test_account_identity_mismatch_fails_closed(tmp_path):
    store = initialized_store(tmp_path)

    other_store = LiveExecutionStateStore(
        path=store.path,
        account_identity="different-account",
    )

    with pytest.raises(LiveStateError, match="identity mismatch"):
        other_store.load()


def test_corrupt_json_state_fails_closed(tmp_path):
    store = initialized_store(tmp_path)

    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write("{invalid json")

    with pytest.raises(LiveStateError, match="unreadable"):
        store.load()


def test_state_integrity_tampering_fails_closed(tmp_path):
    store = initialized_store(tmp_path)

    with open(store.path, "r", encoding="utf-8") as handle:
        state = json.load(handle)

    state["sequence"] = 999

    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)

    with pytest.raises(LiveStateError, match="integrity hash"):
        store.load()


def test_record_fills_persists_positions_and_sequence(tmp_path):
    store = initialized_store(tmp_path)

    fills = [
        fill("1", "BUY_OPEN", "2", "100", 1000),
        fill("2", "SELL_CLOSE", "0.5", "105", 2000),
    ]

    updated = store.record_fills(fills, now_ms=3000)
    loaded = store.load()

    assert updated["sequence"] == 1
    assert loaded["sequence"] == 1
    assert loaded["positions"][SYMBOL]["side"] == "LONG"
    assert loaded["positions"][SYMBOL]["quantity"] == "1.5"
    assert loaded["processed_trade_ids"] == ["1", "2"]


def test_fill_history_regression_is_rejected(tmp_path):
    store = initialized_store(tmp_path)

    complete_history = [
        fill("1", "BUY_OPEN", "2", "100", 1000),
        fill("2", "SELL_CLOSE", "0.5", "105", 2000),
    ]

    store.record_fills(complete_history, now_ms=3000)

    incomplete_history = [
        fill("1", "BUY_OPEN", "2", "100", 1000),
    ]

    with pytest.raises(LiveStateError, match="history regressed"):
        store.record_fills(incomplete_history, now_ms=4000)


def test_reconciliation_failure_halts_state(tmp_path):
    store = initialized_store(tmp_path)

    updated = store.record_reconciliation(
        {
            "ok": False,
            "halt_trading": True,
            "reasons": ["position_quantity_mismatch"],
        },
        now_ms=2000,
    )

    assert updated["halted"] is True
    assert updated["halt_reason"] == "reconciliation_failed"


def test_successful_reconciliation_clears_initial_halt(tmp_path):
    store = initialized_store(tmp_path)

    updated = store.record_reconciliation(
        {
            "ok": True,
            "halt_trading": False,
            "reasons": [],
        },
        now_ms=2000,
    )

    assert updated["halted"] is False
    assert updated["halt_reason"] is None


def test_successful_reconciliation_does_not_clear_existing_halt(tmp_path):
    store = initialized_store(tmp_path)

    store.halt("manual_safety_halt", now_ms=2000)

    updated = store.record_reconciliation(
        {
            "ok": True,
            "halt_trading": False,
            "reasons": [],
        },
        now_ms=3000,
    )

    assert updated["halted"] is True
    assert updated["halt_reason"] == "manual_safety_halt"


def test_invalid_reconciliation_result_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError):
        store.record_reconciliation(
            {
                "ok": "yes",
                "halt_trading": False,
            },
            now_ms=2000,
        )


def test_manual_halt_is_persisted(tmp_path):
    store = initialized_store(tmp_path)

    updated = store.halt(
        "operator_requested_stop",
        now_ms=2000,
    )

    loaded = store.load()

    assert updated["halted"] is True
    assert loaded["halted"] is True
    assert loaded["halt_reason"] == "operator_requested_stop"


def test_balance_snapshot_is_persisted(tmp_path):
    store = initialized_store(tmp_path)

    updated = store.record_balance_snapshot(
        {
            "asset": "USDT",
            "wallet_balance": "100",
            "available_balance": "80",
            "exchange_timestamp_ms": 123456,
        },
        now_ms=2000,
    )

    assert updated["balance_snapshot"]["asset"] == "USDT"
    assert updated["balance_snapshot"]["wallet_balance"] == "100"
    assert updated["balance_snapshot"]["available_balance"] == "80"


def test_available_balance_cannot_exceed_wallet_balance(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="exceeds wallet"):
        store.record_balance_snapshot(
            {
                "asset": "USDT",
                "wallet_balance": "10",
                "available_balance": "11",
            },
            now_ms=2000,
        )


def test_negative_balance_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError):
        store.record_balance_snapshot(
            {
                "asset": "USDT",
                "wallet_balance": "-10",
                "available_balance": "0",
            },
            now_ms=2000,
        )


def test_state_sequence_increments_for_each_update(tmp_path):
    store = initialized_store(tmp_path)

    first = store.halt("first", now_ms=2000)
    second = store.halt("second", now_ms=3000)

    assert first["sequence"] == 1
    assert second["sequence"] == 2