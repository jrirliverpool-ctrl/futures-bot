
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from live_execution_state import (
    BALANCE_SOURCE,
    POSITION_SOURCE,
    LiveExecutionStateStore,
    LiveStateError,
    create_initial_state,
    derive_positions_from_fills,
)


ACCOUNT = "toobit-usdtm-test-account"
STRATEGY_HASH = "sha256:test-strategy-hash"
SYMBOL = "BTC-SWAP-USDT"
NOW = 2_000_000


def fill(
    trade_id,
    side,
    qty,
    price,
    timestamp_ms,
    symbol=SYMBOL,
    exchange_sequence=None,
):
    result = {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "timestamp_ms": timestamp_ms,
    }

    if exchange_sequence is not None:
        result["exchange_sequence"] = exchange_sequence

    return result


def make_store(tmp_path):
    return LiveExecutionStateStore(
        path=str(tmp_path / "private" / "live_state.json"),
        account_identity=ACCOUNT,
    )


def initialized_store(tmp_path):
    store = make_store(tmp_path)

    store.initialize(
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=NOW - 1000,
    )

    return store


def balance_snapshot(timestamp=None, source=BALANCE_SOURCE):
    return {
        "source": source,
        "asset": "USDT",
        "wallet_balance": "100",
        "available_balance": "80",
        "exchange_timestamp_ms": NOW if timestamp is None else timestamp,
    }


def position_snapshot(
    positions=None,
    expected=None,
    checked=None,
    timestamp=None,
    ok=True,
    halt=False,
    reasons=None,
    source=POSITION_SOURCE,
):
    symbols = [SYMBOL] if expected is None else expected

    return {
        "source": source,
        "ok": ok,
        "halt_trading": halt,
        "reasons": [] if reasons is None else reasons,
        "symbols_expected": symbols,
        "symbols_checked": symbols if checked is None else checked,
        "exchange_positions": {} if positions is None else positions,
        "exchange_timestamp_ms": NOW if timestamp is None else timestamp,
    }


def ready_store(tmp_path, fills=None):
    store = initialized_store(tmp_path)

    if fills is not None:
        store.record_fills(fills, now_ms=NOW - 100)

    store.record_balance_snapshot(
        balance_snapshot(),
        now_ms=NOW,
    )

    return store


def test_initial_state_starts_halted():
    state = create_initial_state(
        ACCOUNT,
        "DEMO",
        STRATEGY_HASH,
        now_ms=NOW,
    )

    assert state["halted"] is True
    assert state["positions"] == {}
    assert state["processed_trade_ids"] == []
    assert state["sequence"] == 0


def test_invalid_environment_rejected():
    with pytest.raises(ValueError):
        create_initial_state(
            ACCOUNT, "PAPER", STRATEGY_HASH, now_ms=NOW
        )


def test_invalid_account_identity_rejected():
    with pytest.raises(LiveStateError):
        create_initial_state("", "DEMO", STRATEGY_HASH, now_ms=NOW)


def test_long_weighted_average_and_partial_close():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 300),
        fill("2", "BUY_OPEN", "1", "120", NOW - 200),
        fill("3", "SELL_CLOSE", "0.5", "110", NOW - 100),
    ]

    result = derive_positions_from_fills(fills)

    assert result[SYMBOL]["side"] == "LONG"
    assert result[SYMBOL]["quantity"] == "1.5"
    assert float(result[SYMBOL]["avg_price"]) == pytest.approx(110)


def test_long_full_close():
    fills = [
        fill("1", "BUY_OPEN", "2", "100", NOW - 200),
        fill("2", "SELL_CLOSE", "2", "105", NOW - 100),
    ]

    assert derive_positions_from_fills(fills) == {}


def test_short_partial_close():
    fills = [
        fill("1", "SELL_OPEN", "3", "100", NOW - 200),
        fill("2", "BUY_CLOSE", "1", "90", NOW - 100),
    ]

    result = derive_positions_from_fills(fills)

    assert result[SYMBOL]["side"] == "SHORT"
    assert result[SYMBOL]["quantity"] == "2"
    assert result[SYMBOL]["avg_price"] == "100"


def test_multiple_symbols_are_independent():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 200, "BTC-SWAP-USDT"),
        fill("2", "SELL_OPEN", "2", "50", NOW - 100, "ETH-SWAP-USDT"),
    ]

    result = derive_positions_from_fills(fills)

    assert result["BTC-SWAP-USDT"]["side"] == "LONG"
    assert result["ETH-SWAP-USDT"]["side"] == "SHORT"


def test_duplicate_trade_id_rejected():
    fills = [
        fill("same", "BUY_OPEN", "1", "100", NOW - 200),
        fill("same", "BUY_OPEN", "1", "100", NOW - 100),
    ]

    with pytest.raises(LiveStateError, match="duplicate trade_id"):
        derive_positions_from_fills(fills)


def test_same_timestamp_same_symbol_without_sequence_halts():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 100),
        fill("2", "SELL_CLOSE", "1", "101", NOW - 100),
    ]

    with pytest.raises(LiveStateError, match="ambiguous"):
        derive_positions_from_fills(fills)


def test_same_timestamp_with_exchange_sequence_is_ordered():
    fills = [
        fill("close", "SELL_CLOSE", "1", "101", NOW - 100,
             exchange_sequence=20),
        fill("open", "BUY_OPEN", "1", "100", NOW - 100,
             exchange_sequence=10),
    ]

    result = derive_positions_from_fills(fills)

    assert result == {}


def test_duplicate_exchange_sequence_rejected():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 100,
             exchange_sequence=10),
        fill("2", "SELL_CLOSE", "1", "101", NOW - 100,
             exchange_sequence=10),
    ]

    with pytest.raises(LiveStateError, match="duplicate exchange sequence"):
        derive_positions_from_fills(fills)


def test_same_timestamp_different_symbols_is_allowed():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 100, "BTC-SWAP-USDT"),
        fill("2", "SELL_OPEN", "1", "50", NOW - 100, "ETH-SWAP-USDT"),
    ]

    result = derive_positions_from_fills(fills)

    assert len(result) == 2


def test_closing_without_open_rejected():
    with pytest.raises(LiveStateError, match="no matching open"):
        derive_positions_from_fills([
            fill("1", "SELL_CLOSE", "1", "100", NOW - 100)
        ])


def test_close_quantity_exceeding_open_rejected():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 200),
        fill("2", "SELL_CLOSE", "2", "105", NOW - 100),
    ]

    with pytest.raises(LiveStateError, match="exceeds position"):
        derive_positions_from_fills(fills)


def test_opposite_open_without_close_rejected():
    fills = [
        fill("1", "BUY_OPEN", "1", "100", NOW - 200),
        fill("2", "SELL_OPEN", "1", "105", NOW - 100),
    ]

    with pytest.raises(LiveStateError, match="opposite-side"):
        derive_positions_from_fills(fills)


@pytest.mark.parametrize(
    "bad",
    [
        fill("1", "BAD", "1", "100", NOW - 100),
        fill("2", "BUY_OPEN", "0", "100", NOW - 100),
        fill("3", "BUY_OPEN", "1", "0", NOW - 100),
        fill("4", "BUY_OPEN", "NaN", "100", NOW - 100),
        fill("5", "BUY_OPEN", "1", "Infinity", NOW - 100),
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
def test_invalid_fill_rejected(bad):
    with pytest.raises(LiveStateError):
        derive_positions_from_fills([bad])


def test_store_initializes_and_loads(tmp_path):
    store = initialized_store(tmp_path)
    state = store.load()

    assert state["environment"] == "DEMO"
    assert state["account_identity"] == ACCOUNT
    assert state["halted"] is True


def test_missing_state_fails_closed(tmp_path):
    with pytest.raises(LiveStateError, match="missing"):
        make_store(tmp_path).load()


def test_initialize_refuses_overwrite(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="already exists"):
        store.initialize("DEMO", STRATEGY_HASH, now_ms=NOW)


def test_account_identity_mismatch(tmp_path):
    store = initialized_store(tmp_path)

    other = LiveExecutionStateStore(
        store.path,
        "different-account",
    )

    with pytest.raises(LiveStateError, match="identity mismatch"):
        other.load()


def test_corrupt_json_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write("{invalid")

    with pytest.raises(LiveStateError, match="unreadable"):
        store.load()


def test_state_hash_tampering_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with open(store.path, "r", encoding="utf-8") as handle:
        state = json.load(handle)

    state["sequence"] = 999

    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)

    with pytest.raises(LiveStateError, match="integrity hash"):
        store.load()


def test_fill_history_persisted(tmp_path):
    store = initialized_store(tmp_path)

    fills = [
        fill("1", "BUY_OPEN", "2", "100", NOW - 200),
        fill("2", "SELL_CLOSE", "0.5", "105", NOW - 100),
    ]

    updated = store.record_fills(fills, now_ms=NOW)

    assert updated["sequence"] == 1
    assert updated["positions"][SYMBOL]["quantity"] == "1.5"
    assert updated["processed_trade_ids"] == ["1", "2"]


def test_fill_history_regression_rejected(tmp_path):
    store = initialized_store(tmp_path)

    store.record_fills([
        fill("1", "BUY_OPEN", "2", "100", NOW - 200),
        fill("2", "SELL_CLOSE", "0.5", "105", NOW - 100),
    ], now_ms=NOW)

    with pytest.raises(LiveStateError, match="regressed"):
        store.record_fills([
            fill("1", "BUY_OPEN", "2", "100", NOW - 200),
        ], now_ms=NOW + 1)


def test_balance_snapshot_persisted(tmp_path):
    store = initialized_store(tmp_path)

    result = store.record_balance_snapshot(
        balance_snapshot(),
        now_ms=NOW,
    )

    assert result["balance_snapshot"]["asset"] == "USDT"
    assert result["balance_snapshot"]["wallet_balance"] == "100"
    assert result["balance_snapshot"]["received_at_ms"] == NOW


def test_stale_balance_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="stale"):
        store.record_balance_snapshot(
            balance_snapshot(NOW - 30_001),
            now_ms=NOW,
        )


def test_future_balance_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="future"):
        store.record_balance_snapshot(
            balance_snapshot(NOW + 10_000),
            now_ms=NOW,
        )


def test_untrusted_balance_source_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="untrusted"):
        store.record_balance_snapshot(
            balance_snapshot(source="UNKNOWN"),
            now_ms=NOW,
        )


def test_available_balance_cannot_exceed_wallet(tmp_path):
    store = initialized_store(tmp_path)
    snapshot = balance_snapshot()
    snapshot["available_balance"] = "101"

    with pytest.raises(LiveStateError, match="exceeds wallet"):
        store.record_balance_snapshot(snapshot, now_ms=NOW)


def test_negative_balance_rejected(tmp_path):
    store = initialized_store(tmp_path)
    snapshot = balance_snapshot()
    snapshot["wallet_balance"] = "-10"

    with pytest.raises(LiveStateError):
        store.record_balance_snapshot(snapshot, now_ms=NOW)


def test_successful_reconciliation_clears_initial_halt(tmp_path):
    store = ready_store(tmp_path)

    result = store.record_reconciliation(
        position_snapshot(),
        now_ms=NOW,
    )

    assert result["halted"] is False
    assert result["halt_reason"] is None


def test_reconciliation_requires_fresh_balance(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(LiveStateError, match="fresh balance"):
        store.record_reconciliation(
            position_snapshot(),
            now_ms=NOW,
        )


def test_reconciliation_requires_complete_symbol_coverage(tmp_path):
    store = ready_store(tmp_path)

    result = position_snapshot(
        expected=["BTC-SWAP-USDT", "ETH-SWAP-USDT"],
        checked=["BTC-SWAP-USDT"],
    )

    with pytest.raises(LiveStateError, match="coverage incomplete"):
        store.record_reconciliation(result, now_ms=NOW)


def test_reconciliation_rejects_stale_exchange_data(tmp_path):
    store = ready_store(tmp_path)

    with pytest.raises(LiveStateError, match="stale"):
        store.record_reconciliation(
            position_snapshot(timestamp=NOW - 30_001),
            now_ms=NOW,
        )


def test_reconciliation_rejects_untrusted_source(tmp_path):
    store = ready_store(tmp_path)

    with pytest.raises(LiveStateError, match="untrusted"):
        store.record_reconciliation(
            position_snapshot(source="UNKNOWN"),
            now_ms=NOW,
        )


def test_reconciliation_detects_position_mismatch(tmp_path):
    fills = [
        fill("1", "BUY_OPEN", "2", "100", NOW - 200),
    ]

    store = ready_store(tmp_path, fills)

    exchange = {
        SYMBOL: {
            "side": "LONG",
            "quantity": "1",
            "avg_price": "100",
        }
    }

    with pytest.raises(LiveStateError, match="do not match"):
        store.record_reconciliation(
            position_snapshot(positions=exchange),
            now_ms=NOW,
        )


def test_reconciliation_accepts_matching_position(tmp_path):
    fills = [
        fill("1", "BUY_OPEN", "2", "100", NOW - 200),
    ]

    store = ready_store(tmp_path, fills)

    exchange = {
        SYMBOL: {
            "side": "LONG",
            "quantity": "2",
            "avg_price": "100",
        }
    }

    result = store.record_reconciliation(
        position_snapshot(positions=exchange),
        now_ms=NOW,
    )

    assert result["halted"] is False


def test_reconciliation_cannot_claim_success_with_failure_flag(tmp_path):
    store = ready_store(tmp_path)

    result = store.record_reconciliation(
        position_snapshot(ok=False, halt=True),
        now_ms=NOW,
    )

    assert result["halted"] is True


def test_reconciliation_with_reasons_does_not_clear_halt(tmp_path):
    store = ready_store(tmp_path)

    result = store.record_reconciliation(
        position_snapshot(reasons=["warning"]),
        now_ms=NOW,
    )

    assert result["halted"] is True


def test_manual_halt_is_not_cleared(tmp_path):
    store = ready_store(tmp_path)

    store.halt("manual_safety_halt", now_ms=NOW)

    result = store.record_reconciliation(
        position_snapshot(),
        now_ms=NOW,
    )

    assert result["halted"] is True
    assert result["halt_reason"] == "manual_safety_halt"


def test_reconciliation_invalid_boolean_rejected(tmp_path):
    store = ready_store(tmp_path)

    result = position_snapshot()
    result["ok"] = "yes"

    with pytest.raises(LiveStateError):
        store.record_reconciliation(result, now_ms=NOW)


def test_manual_halt_persists(tmp_path):
    store = initialized_store(tmp_path)

    result = store.halt("operator_requested_stop", now_ms=NOW)

    assert result["halted"] is True
    assert store.load()["halt_reason"] == "operator_requested_stop"


def test_state_timestamp_cannot_regress(tmp_path):
    store = initialized_store(tmp_path)

    store.halt("first", now_ms=NOW)

    with pytest.raises(LiveStateError, match="regressed"):
        store.halt("second", now_ms=NOW - 1)


def test_sequence_increments(tmp_path):
    store = initialized_store(tmp_path)

    first = store.halt("first", now_ms=NOW)
    second = store.halt("second", now_ms=NOW + 1)

    assert first["sequence"] == 1
    assert second["sequence"] == 2
