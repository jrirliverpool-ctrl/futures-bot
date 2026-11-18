
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

from toobit_v2_algo_snapshot import (
    AlgoOrderSnapshotError,
    fetch_stable_v2_algo_order_snapshot,
)


SYMBOL = "BTC-SWAP-USDT"


def stop_row(**overrides):
    row = {
        "orderId": "1001",
        "clientOrderId": "sl-1",
        "symbol": SYMBOL,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_MARKET",
        "status": "ORDER_NEW",
        "origQty": "2",
        "executedQty": "0",
        "stopPrice": "95000",
        "triggerBy": "MARK_PRICE",
    }

    row.update(overrides)
    return row


def tp_row(**overrides):
    row = {
        "orderId": "1002",
        "clientOrderId": "tp-1",
        "symbol": SYMBOL,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_PROFIT_LOSS_MARKET",
        "status": "ORDER_NEW",
        "origQty": "1",
        "executedQty": "0",
        "stopPrice": "110000",
        "triggerBy": "MARK_PRICE",
        "stopOrderType": "TAKE_PROFIT",
        "stopType": "FIXED_STOP",
    }

    row.update(overrides)
    return row


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {
            "STOP": [stop_row()],
            "STOP_PROFIT_LOSS": [tp_row()],
        }

        self.calls = []

    def _request_v2_get(self, path, params):
        self.calls.append(
            (path, dict(params))
        )

        category = params["stopCategory"]
        value = self.responses[category]

        if isinstance(value, Exception):
            raise value

        if callable(value):
            count = len([
                call
                for call in self.calls
                if call[1]["stopCategory"] == category
            ])

            return value(count)

        return value


def test_stable_snapshot_reads_both_categories_twice():
    client = FakeClient()

    result = fetch_stable_v2_algo_order_snapshot(
        client,
        symbol=SYMBOL,
    )

    assert result["stable"] is True

    assert (
        result["complete_within_documented_page_bound"]
        is True
    )

    assert [
        row["orderId"]
        for row in result["orders"]
    ] == ["1001", "1002"]

    assert result["categories_checked"] == [
        "STOP",
        "STOP_PROFIT_LOSS",
    ]

    assert result["scans"] == 2
    assert result["read_only"] is True

    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0

    assert len(client.calls) == 4

    assert all(
        call[0] == "/api/v2/futures/open-algo-orders"
        for call in client.calls
    )

    assert all(
        call[1]["category"] == "USDT"
        for call in client.calls
    )

    assert all(
        call[1]["symbol"] == SYMBOL
        for call in client.calls
    )


def test_default_limit_does_not_omit_tpsl_category():
    client = FakeClient()

    fetch_stable_v2_algo_order_snapshot(
        client,
        symbol=SYMBOL,
    )

    assert {
        call[1]["stopCategory"]
        for call in client.calls
    } == {
        "STOP",
        "STOP_PROFIT_LOSS",
    }


def test_full_page_fails_closed():
    client = FakeClient({
        "STOP": [
            stop_row(),
            stop_row(orderId="1003"),
        ],
        "STOP_PROFIT_LOSS": [],
    })

    with pytest.raises(
        AlgoOrderSnapshotError,
        match="page_may_be_truncated",
    ):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
            limit=2,
        )

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        stop_row(orderId="not-numeric"),
        stop_row(symbol="ETH-SWAP-USDT"),
        stop_row(positionSide="BOTH"),
        stop_row(status="CANCELED"),
        stop_row(origQty="NaN"),
        stop_row(type="STOP_PROFIT_LOSS"),
        stop_row(stopPrice="0"),
    ],
)
def test_invalid_plan_rows_fail_closed(bad):
    client = FakeClient({
        "STOP": [bad],
        "STOP_PROFIT_LOSS": [],
    })

    with pytest.raises(AlgoOrderSnapshotError):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
        )


def test_invalid_tpsl_row_fails_closed():
    client = FakeClient({
        "STOP": [],
        "STOP_PROFIT_LOSS": [
            tp_row(stopOrderType="UNKNOWN")
        ],
    })

    with pytest.raises(AlgoOrderSnapshotError):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
        )


def test_unstable_two_scan_snapshot_fails_closed():
    client = FakeClient({
        "STOP": lambda count: [
            stop_row(
                stopPrice=(
                    "95000" if count == 1 else "94000"
                )
            )
        ],
        "STOP_PROFIT_LOSS": [],
    })

    with pytest.raises(
        AlgoOrderSnapshotError,
        match="changed_between_reads",
    ):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
        )


def test_request_error_fails_closed():
    client = FakeClient({
        "STOP": TimeoutError("timeout"),
        "STOP_PROFIT_LOSS": [],
    })

    with pytest.raises(
        AlgoOrderSnapshotError,
        match="request_failed",
    ):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
        )


def test_duplicate_id_across_categories_fails_closed():
    client = FakeClient({
        "STOP": [
            stop_row(orderId="1001")
        ],
        "STOP_PROFIT_LOSS": [
            tp_row(orderId="1001")
        ],
    })

    with pytest.raises(
        AlgoOrderSnapshotError,
        match="across_categories",
    ):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
        )


def test_invalid_inputs_rejected_before_network():
    client = FakeClient()

    with pytest.raises(ValueError):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol="BTCUSDT",
        )

    with pytest.raises(ValueError):
        fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=SYMBOL,
            limit=True,
        )

    assert client.calls == []
