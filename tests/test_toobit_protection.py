
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

from toobit_protection import (
    set_and_verify_fixed_protection,
    ProtectionError,
)


class FakeClient:
    def __init__(
        self,
        post=None,
        rows=None,
        get_error=False,
        post_error=False,
    ):
        self.post = post or {
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "stopLoss": "95",
            "takeProfit": "110",
            "_http_status": 200,
        }

        self.rows = rows if rows is not None else [
            {
                "symbol": "BTC-SWAP-USDT",
                "type": "STOP_PROFIT_LOSS",
                "orderType": "STOP_LONG_LOSS",
                "side": "SELL_CLOSE",
                "status": "ORDER_NEW",
                "stopPrice": "95",
                "origQty": "2",
            },
            {
                "symbol": "BTC-SWAP-USDT",
                "type": "STOP_PROFIT_LOSS",
                "orderType": "STOP_LONG_PROFIT",
                "side": "SELL_CLOSE",
                "status": "ORDER_NEW",
                "stopPrice": "110",
                "origQty": "2",
            },
        ]

        self.get_error = get_error
        self.post_error = post_error
        self.calls = []

    def _request(self, method, path, params):
        self.calls.append((method, path, params))

        if method == "POST":
            if self.post_error:
                raise TimeoutError("simulated ambiguous timeout")
            return self.post

        if self.get_error:
            return {"_transport": "UNKNOWN"}

        return self.rows


def run(client):
    return set_and_verify_fixed_protection(
        client,
        symbol="BTC-SWAP-USDT",
        side="LONG",
        quantity="2",
        stop_loss="95",
        take_profit="110",
        trigger_by="MARK_PRICE",
    )


def test_fixed_sl_tp_set_and_verified():
    client = FakeClient()

    result = run(client)

    assert result["halt_trading"] is False
    assert result["stop_loss_verified"] is True
    assert result["take_profit_verified"] is True
    assert result["verified_open_protection_orders"] == 2

    assert [call[0] for call in client.calls] == [
        "POST",
        "GET",
    ]

    assert client.calls[0][2]["slSize"] == "2"
    assert client.calls[0][2]["tpSize"] == "2"


def test_ambiguous_post_is_not_retried():
    client = FakeClient(post_error=True)

    with pytest.raises(ProtectionError):
        run(client)

    assert len(client.calls) == 1


def test_missing_stop_loss_order_fails_closed():
    client = FakeClient(rows=[])

    with pytest.raises(ProtectionError):
        run(client)


def test_wrong_quantity_fails_closed():
    client = FakeClient(
        rows=[
            {
                "symbol": "BTC-SWAP-USDT",
                "type": "STOP_PROFIT_LOSS",
                "orderType": "STOP_LONG_LOSS",
                "side": "SELL_CLOSE",
                "status": "ORDER_NEW",
                "stopPrice": "95",
                "origQty": "1",
            }
        ]
    )

    with pytest.raises(ProtectionError):
        run(client)


def test_get_failure_after_post_fails_closed():
    client = FakeClient(get_error=True)

    with pytest.raises(ProtectionError):
        run(client)

    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "INVALID"},
        {"quantity": "0"},
        {"stop_loss": "-1"},
        {"take_profit": "95"},
        {"symbol": "BTCUSDT"},
    ],
)
def test_invalid_input_does_not_call_exchange(kwargs):
    client = FakeClient()

    args = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "quantity": "2",
        "stop_loss": "95",
        "take_profit": "110",
    }

    args.update(kwargs)

    with pytest.raises(ValueError):
        set_and_verify_fixed_protection(
            client,
            **args,
        )

    assert client.calls == []


def test_post_echo_mismatch_fails_closed():
    client = FakeClient(
        post={
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "stopLoss": "94",
            "takeProfit": "110",
            "_http_status": 200,
        }
    )

    with pytest.raises(ProtectionError):
        run(client)

    assert len(client.calls) == 1
