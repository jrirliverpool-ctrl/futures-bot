import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_recovery_probe import OrderRecoveryProbe


def trade(**overrides):
    row = {
        "time": "1790000000123",
        "id": "trade-1",
        "orderId": "order-1",
        "symbol": "BTC-SWAP-USDT",
        "price": "100",
        "qty": "1",
        "commissionAsset": "USDT",
        "commission": "0.01",
        "makerRebate": "0",
        "type": "LIMIT",
        "isMaker": False,
        "side": "BUY_OPEN",
        "realizedPnl": "0",
        "ticketId": "ticket-1",
    }

    row.update(overrides)
    return row


def order(status="FILLED", **overrides):
    row = {
        "orderId": "order-1",
        "clientOrderId": "client-1",
        "symbol": "BTC-SWAP-USDT",
        "side": "BUY_OPEN",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "100",
        "status": status,
    }

    row.update(overrides)
    return row


def record(status="SUBMISSION_STARTED", **overrides):
    row = {
        "client_order_id": "client-1",
        "status": status,
        "created_at_ms": 1790000000000,
        "exchange_order_id": (
            "order-1"
            if status == "EXCHANGE_ACKNOWLEDGED"
            else None
        ),
        "intent": {
            "client_order_id": "client-1",
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "intent": "ENTRY",
            "order_type": "MARKET",
            "qty": "1",
            "price": None,
            "leverage": 3,
            "signal_id": "signal-1",
            "strategy_hash": "sha256:" + "a" * 64,
        },
    }

    row.update(overrides)
    return row


class FakeStore:
    def __init__(self, records):
        self.records = records
        self.write_calls = 0

    def recovery_required(self):
        return self.records

    def __getattr__(self, name):
        if name in {
            "create_intent",
            "mark_submission_started",
            "mark_submission_unknown",
            "acknowledge",
            "mark_reconciled",
        }:
            def forbidden(*args, **kwargs):
                self.write_calls += 1
                raise AssertionError(
                    "probe must not mutate the journal"
                )

            return forbidden

        raise AttributeError(name)


class FakeClient:
    def __init__(
        self,
        snapshot=None,
        trades=None,
        response_status="FILLED",
        error=None,
    ):
        self.snapshot = snapshot or order(response_status)
        self.trades = [trade()] if trades is None else trades
        self.response_status = response_status
        self.error = error
        self.calls = []

    def get_order_status(self, symbol, client_order_id):
        self.calls.append(
            (
                "get_order_status",
                symbol,
                client_order_id,
            )
        )

        if self.error:
            raise self.error

        return SimpleNamespace(
            status=self.response_status,
            raw_response={"data": self.snapshot},
        )

    def get_user_trades(
        self,
        symbol,
        start_time=None,
        end_time=None,
        limit=1000,
    ):
        self.calls.append(
            (
                "get_user_trades",
                symbol,
                start_time,
                end_time,
                limit,
            )
        )

        if self.error:
            raise self.error

        return self.trades


def test_successful_probe_is_read_only_and_keeps_trading_halted():
    store = FakeStore([record()])
    client = FakeClient()

    result = OrderRecoveryProbe(
        client,
        store,
    ).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["halt_trading"] is True
    assert result["journal_mutated"] is False
    assert result["trading_state_mutated"] is False
    assert result["orders_submitted"] == 0
    assert result["orders_canceled"] == 0

    assert (
        result["items"][0]["can_be_marked_reconciled"]
        is True
    )

    assert client.calls[0] == (
        "get_order_status",
        "BTC-SWAP-USDT",
        "client-1",
    )

    assert client.calls[1][0] == "get_user_trades"

    assert client.calls[1][2:4] == (
        1790000000000,
        1790000005000,
    )

    assert store.write_calls == 0


def test_intent_created_is_not_queried_or_declared_safe():
    store = FakeStore([
        record(status="INTENT_CREATED")
    ])

    client = FakeClient()

    result = OrderRecoveryProbe(
        client,
        store,
    ).run(now_ms=1790000005000)

    assert result["ok"] is False

    assert (
        result["items"][0]["reason"]
        == "intent_created_without_submission_started"
    )

    assert client.calls == []


@pytest.mark.parametrize(
    "snapshot,reason",
    [
        (
            order(clientOrderId="wrong"),
            "client_order_id_mismatch",
        ),
        (
            order(symbol="ETH-SWAP-USDT"),
            "exchange_symbol_mismatch",
        ),
        (
            order(side="SELL_OPEN"),
            "exchange_side_mismatch",
        ),
        (
            order(origQty="2"),
            "exchange_original_quantity_mismatch",
        ),
    ],
)
def test_order_identity_mismatch_fails_closed(snapshot, reason):
    result = OrderRecoveryProbe(
        FakeClient(snapshot=snapshot),
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["items"][0]["reason"] == reason


def test_acknowledged_exchange_order_id_must_match():
    store = FakeStore([
        record(
            status="EXCHANGE_ACKNOWLEDGED",
            exchange_order_id="other-id",
        )
    ])

    result = OrderRecoveryProbe(
        FakeClient(),
        store,
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "exchange_order_id_mismatch"
    )


def test_unknown_exchange_response_does_not_query_trades():
    client = FakeClient(response_status="UNKNOWN")

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "exchange_order_status_unknown"
    )

    assert len(client.calls) == 1


def test_trade_quantity_mismatch_fails_closed():
    client = FakeClient(
        trades=[trade(qty="0.5")]
    )

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "fill_reconciliation_failed"
    )


def test_nonterminal_exchange_order_never_passes_recovery():
    snapshot = order(
        status="PARTIALLY_FILLED",
        executedQty="0.5",
    )

    client = FakeClient(
        snapshot=snapshot,
        trades=[trade(qty="0.5")],
        response_status="PARTIALLY_FILLED",
    )

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "exchange_order_not_terminal"
    )


def test_full_trade_page_is_treated_as_potentially_truncated():
    client = FakeClient(
        trades=[
            trade(id=f"t-{i}")
            for i in range(1000)
        ]
    )

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "user_trades_result_may_be_truncated"
    )


def test_exchange_query_exception_fails_closed():
    client = FakeClient(
        error=TimeoutError("timeout")
    )

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "read_only_probe_error"
    )


def test_trades_for_other_orders_are_not_counted():
    client = FakeClient(
        trades=[
            trade(orderId="different")
        ]
    )

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "fill_reconciliation_failed"
    )


def test_missing_trade_list_or_invalid_record_fails_closed():
    store = FakeStore([
        {
            "client_order_id": "bad",
            "status": "SUBMISSION_STARTED",
        }
    ])

    result = OrderRecoveryProbe(
        FakeClient(),
        store,
    ).run(now_ms=1790000005000)

    assert result["ok"] is False

    assert (
        result["items"][0]["reason"]
        == "invalid_intent_record"
    )


def test_empty_unresolved_journal_reports_no_work():
    result = OrderRecoveryProbe(
        FakeClient(),
        FakeStore([]),
    ).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["unresolved_count"] == 0


def test_response_status_must_match_raw_snapshot_status():
    client = FakeClient(
        snapshot=order(status="FILLED"),
        response_status="CANCELED",
    )

    result = OrderRecoveryProbe(
        client,
        FakeStore([record()]),
    ).run(now_ms=1790000005000)

    assert (
        result["items"][0]["reason"]
        == "exchange_status_wrapper_mismatch"
    )

    assert len(client.calls) == 1


def test_unreadable_intent_journal_fails_closed_without_exchange_calls():

    class BrokenStore:
        def recovery_required(self):
            raise ValueError("corrupt journal")

    client = FakeClient()

    result = OrderRecoveryProbe(
        client,
        BrokenStore(),
    ).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True

    assert (
        result["items"][0]["reason"]
        == "intent_journal_unreadable"
    )

    assert client.calls == []