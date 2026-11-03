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
        post_error=False,
        get_error=False,
    ):
        # Realistic success envelope: no echo of submitted parameters.
        self.post = (
            post
            if post is not None
            else {
                "code": 200,
                "msg": "success",
                "_http_status": 200,
            }
        )

        self.rows = (
            rows
            if rows is not None
            else [
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
                    "origQty": "1",
                },
            ]
        )

        self.post_error = post_error
        self.get_error = get_error
        self.calls = []

    def _request(self, method, path, params):
        self.calls.append((method, path, params))

        if method == "POST":
            if self.post_error:
                raise TimeoutError()
            return self.post

        if self.get_error:
            return {"_transport": "UNKNOWN"}

        return self.rows


def call(client, **overrides):
    args = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "quantity": "2",
        "stop_loss": "95",
        "take_profit": "110",
        "take_profit_quantity": "1",
    }

    args.update(overrides)

    return set_and_verify_fixed_protection(
        client,
        **args,
    )


def test_success_envelope_verified_from_exchange_orders():
    client = FakeClient()

    result = call(client)

    assert result["halt_trading"] is False
    assert result["stop_loss_quantity"] == "2"
    assert result["take_profit_quantity"] == "1"
    assert result["stop_loss_verified"] is True
    assert result["take_profit_verified"] is True

    assert len(client.calls) == 2

    assert client.calls[0][2]["slSize"] == "2"
    assert client.calls[0][2]["tpSize"] == "1"


def test_ambiguous_post_never_retried():
    client = FakeClient(post_error=True)

    with pytest.raises(ProtectionError):
        call(client)

    assert len(client.calls) == 1


def test_http_200_but_exchange_error_fails():
    client = FakeClient(
        post={
            "code": -1,
            "msg": "error",
            "_http_status": 200,
        }
    )

    with pytest.raises(ProtectionError):
        call(client)

    assert len(client.calls) == 1


def test_missing_sl_fails_closed():
    client = FakeClient(rows=[])

    with pytest.raises(ProtectionError):
        call(client)


def test_wrong_tp_size_fails_closed():
    client = FakeClient(
        rows=[
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
    )

    with pytest.raises(ProtectionError):
        call(client)


def test_get_failure_after_post_fails_closed():
    client = FakeClient(get_error=True)

    with pytest.raises(ProtectionError):
        call(client)

    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "take_profit": "110",
            "take_profit_quantity": None,
        },
        {
            "take_profit": None,
            "take_profit_quantity": "1",
        },
        {
            "take_profit_quantity": "3",
        },
        {
            "quantity": "0",
        },
        {
            "side": "BAD",
        },
        {
            "symbol": "BTCUSDT",
        },
        {
            "take_profit": "95",
        },
    ],
)
def test_invalid_inputs_never_touch_exchange(kwargs):
    client = FakeClient()

    with pytest.raises(ValueError):
        call(client, **kwargs)

    assert client.calls == []


def test_no_tp_is_allowed_with_sl_only():
    rows = [
        {
            "symbol": "BTC-SWAP-USDT",
            "type": "STOP_PROFIT_LOSS",
            "orderType": "STOP_LONG_LOSS",
            "side": "SELL_CLOSE",
            "status": "ORDER_NEW",
            "stopPrice": "95",
            "origQty": "2",
        }
    ]

    client = FakeClient(rows=rows)

    result = call(
        client,
        take_profit=None,
        take_profit_quantity=None,
    )

    assert result["stop_loss_verified"] is True
    assert result["take_profit_verified"] is False