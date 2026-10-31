import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from toobit_client import ToobitClient


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


def sample_trade():
    return {
        "time": "1790000000123", "id": "trade-1", "orderId": "order-1",
        "symbol": "BTC-SWAP-USDT", "price": "100", "qty": "0.4",
        "commissionAsset": "USDT", "commission": "0.01", "makerRebate": "0",
        "type": "LIMIT", "isMaker": False, "side": "BUY_OPEN",
        "realizedPnl": "0", "ticketId": "ticket-1",
    }


@pytest.fixture
def client():
    return ToobitClient(api_key="TEST_KEY", api_secret="TEST_SECRET")


def test_user_trades_accepts_documented_json_array(client, monkeypatch):
    calls = {}

    def fake_get(url, params, headers, timeout):
        calls["url"] = url
        calls["params"] = dict(params)
        return FakeResponse(200, [sample_trade()])

    monkeypatch.setattr("toobit_client.requests.get", fake_get)
    rows = client.get_user_trades(
        "BTC-SWAP-USDT",
        start_time=100,
        end_time=200,
        limit=50,
        from_id=7,
    )

    assert rows == [sample_trade()]
    assert calls["url"].endswith("/api/v1/futures/userTrades")
    assert calls["params"]["symbol"] == "BTC-SWAP-USDT"
    assert calls["params"]["startTime"] == 100
    assert calls["params"]["endTime"] == 200
    assert calls["params"]["fromId"] == 7
    assert calls["params"]["limit"] == 50


def test_user_trades_empty_success_is_empty_list(client, monkeypatch):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *args, **kwargs: FakeResponse(200, []),
    )
    assert client.get_user_trades("BTC-SWAP-USDT") == []


def test_user_trades_http_error_raises_not_empty_list(client, monkeypatch):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *args, **kwargs: FakeResponse(500, {"code": 500, "message": "error"}),
    )
    with pytest.raises(RuntimeError):
        client.get_user_trades("BTC-SWAP-USDT")


@pytest.mark.parametrize("kwargs", [
    {"symbol": ""},
    {"symbol": "BTC-SWAP-USDT", "limit": 0},
    {"symbol": "BTC-SWAP-USDT", "limit": 1001},
    {"symbol": "BTC-SWAP-USDT", "start_time": -1},
    {"symbol": "BTC-SWAP-USDT", "start_time": 200, "end_time": 100},
])
def test_user_trades_invalid_arguments_rejected_without_network(
    client, monkeypatch, kwargs
):
    def forbidden(*args, **kwargs):
        raise AssertionError("network must not be called")

    monkeypatch.setattr("toobit_client.requests.get", forbidden)
    with pytest.raises(ValueError):
        client.get_user_trades(**kwargs)