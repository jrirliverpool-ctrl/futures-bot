
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "scripts"),
)

from fill_ledger import normalize_user_trade
from live_execution_state import LiveExecutionStateStore
from order_intent_store import OrderIntentStore
from order_recovery_probe import OrderRecoveryProbe


SYMBOL = "BTC-SWAP-USDT"
BASE_TIME = 1_790_000_000_000
STRATEGY_HASH = "sha256:" + "a" * 64


def v2_trade(
    trade_id="1001",
    order_id="order-1",
    side="BUY",
    position_side="LONG",
    timestamp=BASE_TIME + 100,
    qty="1",
    price="100",
):
    return {
        "id": trade_id,
        "orderId": order_id,
        "symbol": SYMBOL,
        "time": str(timestamp),
        "side": side,
        "positionSide": position_side,
        "qty": qty,
        "price": price,
        "commission": "0.01",
        "commissionAsset": "USDT",
        "makerRebate": "0",
        "realizedPnl": "0",
        "isMaker": False,
        "type": "MARKET",
    }


def v2_order(
    client_order_id="aq-integration-001",
    order_id="order-1",
    side="BUY",
    position_side="LONG",
    qty="1",
):
    return {
        "orderId": order_id,
        "clientOrderId": client_order_id,
        "symbol": SYMBOL,
        "origQty": qty,
        "executedQty": qty,
        "avgPrice": "100",
        "status": "FILLED",
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
    }


class FakeV2Client:
    def __init__(self, order_data, trades):
        self.order_data = order_data
        self.trades = trades
        self.order_calls = 0
        self.trade_calls = 0

    def get_order_status_v2(self, symbol, client_order_id):
        self.order_calls += 1

        assert symbol == SYMBOL
        assert (
            client_order_id
            == self.order_data["clientOrderId"]
        )

        return SimpleNamespace(
            status=self.order_data["status"],
            raw_response={"data": self.order_data},
        )

    def get_user_trades_v2(
        self,
        symbol,
        start_time=None,
        end_time=None,
        limit=1000,
        from_id=None,
        to_id=None,
    ):
        self.trade_calls += 1

        assert symbol == SYMBOL
        assert start_time is not None
        assert end_time is not None

        rows = list(self.trades)

        if from_id is not None:
            rows = [
                row
                for row in rows
                if int(row["id"]) < from_id
            ]

        return rows[:limit]


def create_acknowledged_intent(
    tmp_path,
    client_order_id="aq-integration-001",
    side="LONG",
    intent_type="ENTRY",
):
    journal = OrderIntentStore(
        str(tmp_path / "private" / "order_intents.jsonl")
    )

    intent = {
        "client_order_id": client_order_id,
        "symbol": SYMBOL,
        "side": side,
        "intent": intent_type,
        "order_type": "MARKET",
        "qty": "1",
        "price": None,
        "leverage": 3,
        "signal_id": "integration-signal-001",
        "strategy_hash": STRATEGY_HASH,
    }

    journal.create_intent(
        intent,
        now_ms=BASE_TIME,
    )

    journal.mark_submission_started(
        client_order_id,
        now_ms=BASE_TIME + 1,
    )

    journal.acknowledge(
        client_order_id,
        "order-1",
        "FILLED",
        now_ms=BASE_TIME + 2,
    )

    return journal


def test_v2_recovery_fill_ledger_and_live_state_are_connected(
    tmp_path,
):
    raw_trade = v2_trade()

    journal = create_acknowledged_intent(tmp_path)

    client = FakeV2Client(
        order_data=v2_order(),
        trades=[raw_trade],
    )

    report = OrderRecoveryProbe(
        client,
        journal,
    ).run(now_ms=BASE_TIME + 5_000)

    assert report["ok"] is True
    assert report["halt_trading"] is True
    assert report["journal_mutated"] is False
    assert report["trading_state_mutated"] is False
    assert report["orders_submitted"] == 0
    assert report["orders_amended"] == 0
    assert report["orders_canceled"] == 0

    reconciliation = report["items"][0]["reconciliation"]

    assert reconciliation["ok"] is True
    assert reconciliation["halt_trading"] is False
    assert reconciliation["fill_count"] == 1

    journal.mark_reconciled(
        "aq-integration-001",
        reconciliation,
        now_ms=BASE_TIME + 5_001,
    )

    assert (
        journal.get("aq-integration-001")["status"]
        == "RECONCILED"
    )

    normalized_fill = normalize_user_trade(raw_trade)

    state_store = LiveExecutionStateStore(
        path=str(tmp_path / "private" / "live_state.json"),
        account_identity="integration-test-account",
    )

    state_store.initialize(
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=BASE_TIME,
    )

    state_store.record_fills(
        [normalized_fill],
        now_ms=BASE_TIME + 6_000,
    )

    state = state_store.load()

    assert state["positions"][SYMBOL]["side"] == "LONG"
    assert state["positions"][SYMBOL]["quantity"] == "1"
    assert state["processed_trade_ids"] == ["1001"]

    assert client.order_calls == 1
    assert client.trade_calls == 1


def test_v2_normalized_fills_rebuild_both_hedge_sides(
    tmp_path,
):
    long_trade = v2_trade(
        trade_id="2001",
        order_id="long-order",
        side="BUY",
        position_side="LONG",
        timestamp=BASE_TIME + 100,
        qty="1",
        price="100",
    )

    short_trade = v2_trade(
        trade_id="2002",
        order_id="short-order",
        side="SELL",
        position_side="SHORT",
        timestamp=BASE_TIME + 200,
        qty="2",
        price="110",
    )

    normalized = [
        normalize_user_trade(long_trade),
        normalize_user_trade(short_trade),
    ]

    store = LiveExecutionStateStore(
        path=str(tmp_path / "private" / "hedge_state.json"),
        account_identity="hedge-integration-test",
    )

    store.initialize(
        environment="DEMO",
        strategy_hash=STRATEGY_HASH,
        now_ms=BASE_TIME,
    )

    store.record_fills(
        normalized,
        now_ms=BASE_TIME + 1_000,
    )

    state = store.load()
    positions = state["positions"][SYMBOL]

    assert positions["LONG"]["quantity"] == "1"
    assert positions["LONG"]["avg_price"] == "100"

    assert positions["SHORT"]["quantity"] == "2"
    assert positions["SHORT"]["avg_price"] == "110"

    assert set(state["processed_trade_ids"]) == {
        "2001",
        "2002",
    }


@pytest.mark.parametrize(
    "intent_side,intent_type,exchange_side,position_side",
    [
        ("LONG", "ENTRY", "BUY", "LONG"),
        ("LONG", "EXIT", "SELL", "LONG"),
        ("SHORT", "ENTRY", "SELL", "SHORT"),
        ("SHORT", "EXIT", "BUY", "SHORT"),
    ],
)
def test_v2_recovery_validates_position_side_mapping(
    tmp_path,
    intent_side,
    intent_type,
    exchange_side,
    position_side,
):
    client_order_id = (
        f"aq-map-{intent_side.lower()}-{intent_type.lower()}"
    )

    trade = v2_trade(
        trade_id="3001",
        side=exchange_side,
        position_side=position_side,
    )

    order_data = v2_order(
        client_order_id=client_order_id,
        side=exchange_side,
        position_side=position_side,
    )

    journal = create_acknowledged_intent(
        tmp_path,
        client_order_id=client_order_id,
        side=intent_side,
        intent_type=intent_type,
    )

    client = FakeV2Client(
        order_data=order_data,
        trades=[trade],
    )

    report = OrderRecoveryProbe(
        client,
        journal,
    ).run(now_ms=BASE_TIME + 5_000)

    assert report["items"][0]["can_be_marked_reconciled"] is True
    assert report["items"][0]["exchange_status"] == "FILLED"
