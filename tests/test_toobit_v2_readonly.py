import hashlib
import hmac
import os
import sys
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

from toobit_client import ToobitClient
from order_recovery_probe import OrderRecoveryProbe


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


def v2_trade(**overrides):
    row = {
        "time": "1790000000123",
        "id": "trade-v2-1",
        "orderId": "order-v2-1",
        "symbol": "BTC-SWAP-USDT",
        "price": "100",
        "qty": "1",
        "commissionAsset": "USDT",
        "commission": "0.01",
        "makerRebate": "0",
        "type": "LIMIT",
        "side": "BUY",
        "positionSide": "LONG",
        "realizedPnl": "0",
        "ticketId": "ticket-v2-1",
        "isMaker": False,
    }
    row.update(overrides)
    return row


def v2_order(**overrides):
    row = {
        "orderId": "order-v2-1",
        "clientOrderId": "client-v2-1",
        "symbol": "BTC-SWAP-USDT",
        "side": "BUY",
        "positionSide": "LONG",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "100",
        "status": "FILLED",
    }
    row.update(overrides)
    return row


@pytest.fixture
def client():
    return ToobitClient(api_key="TEST_KEY", api_secret="TEST_SECRET")


def test_v2_user_trades_reads_documented_data_array(client, monkeypatch):
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = list(params)
        captured["headers"] = headers
        return FakeResponse(200, {"code": 200, "msg": "success",
                                  "data": [v2_trade()]})

    monkeypatch.setattr("toobit_client.requests.get", fake_get)

    rows = client.get_user_trades_v2(
        "BTC-SWAP-USDT",
        start_time=100,
        end_time=200,
        limit=50,
        from_id=7,
    )

    assert rows == [v2_trade()]
    assert captured["url"].endswith("/api/v2/futures/user-trades")

    params = dict(captured["params"])
    assert params["symbol"] == "BTC-SWAP-USDT"
    assert params["category"] == "USDT"
    assert params["startTime"] == 100
    assert params["endTime"] == 200
    assert params["fromId"] == 7
    assert params["limit"] == 50
    assert "signature" in params
    assert captured["headers"]["X-BB-APIKEY"] == "TEST_KEY"


def test_v2_signature_matches_sorted_query_parameters(client, monkeypatch):
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["params"] = list(params)
        return FakeResponse(200, {"code": 200, "data": []})

    monkeypatch.setattr("toobit_client.requests.get", fake_get)

    client.get_user_trades_v2("BTC-SWAP-USDT", limit=10)

    params = dict(captured["params"])
    signature = params.pop("signature")
    query = urlencode(sorted(params.items()))
    expected = hmac.new(
        b"TEST_SECRET", query.encode(), hashlib.sha256
    ).hexdigest()

    assert signature == expected


def test_v2_empty_data_array_is_successful_empty_result(client, monkeypatch):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *a, **k: FakeResponse(200, {"code": 200, "data": []}),
    )
    assert client.get_user_trades_v2("BTC-SWAP-USDT") == []


@pytest.mark.parametrize(
    "body",
    [
        {"code": -1, "msg": "error", "data": []},
        {"code": 200, "msg": "success"},
        {"code": 200, "data": {}},
        {"code": 200, "data": ["not-a-dict"]},
    ],
)
def test_v2_invalid_or_error_response_fails_closed(client, monkeypatch, body):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *a, **k: FakeResponse(200, body),
    )
    with pytest.raises(RuntimeError):
        client.get_user_trades_v2("BTC-SWAP-USDT")


def test_v2_http_error_fails_closed(client, monkeypatch):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *a, **k: FakeResponse(500, {"code": 500, "data": []}),
    )
    with pytest.raises(RuntimeError):
        client.get_user_trades_v2("BTC-SWAP-USDT")


def test_v2_order_status_maps_documented_order_response(client, monkeypatch):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *a, **k: FakeResponse(
            200, {"code": 200, "data": v2_order()}
        ),
    )

    response = client.get_order_status_v2(
        "BTC-SWAP-USDT", "client-v2-1"
    )

    assert response.status == "FILLED"
    assert response.exchange_order_id == "order-v2-1"
    assert response.client_order_id == "client-v2-1"
    assert response.filled_qty == 1.0


def test_v2_order_status_wrong_symbol_becomes_unknown(client, monkeypatch):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *a, **k: FakeResponse(
            200, {"code": 200, "data": v2_order(symbol="ETH-SWAP-USDT")}
        ),
    )

    response = client.get_order_status_v2(
        "BTC-SWAP-USDT", "client-v2-1"
    )
    assert response.status == "UNKNOWN"


class FakeStore:
    def __init__(self, records):
        self.records = records

    def recovery_required(self):
        return self.records

    def __getattr__(self, name):
        if name in {
            "create_intent", "mark_submission_started",
            "mark_submission_unknown", "acknowledge", "mark_reconciled",
        }:
            raise AssertionError("probe must not mutate intent journal")
        raise AttributeError(name)


class FakeV2Client:
    def __init__(self):
        self.calls = []

    def get_order_status_v2(self, symbol, client_order_id):
        self.calls.append(("order_v2", symbol, client_order_id))
        raw = {"code": 200, "data": v2_order()}
        return SimpleNamespace(status="FILLED", raw_response=raw)

    def get_user_trades_v2(
        self, symbol, start_time=None, end_time=None, limit=1000
    ):
        self.calls.append(("trades_v2", symbol, start_time, end_time, limit))
        return [v2_trade()]


def test_probe_uses_v2_read_only_methods_and_keeps_halt_true():
    record = {
        "client_order_id": "client-v2-1",
        "status": "SUBMISSION_STARTED",
        "created_at_ms": 1790000000000,
        "exchange_order_id": None,
        "intent": {
            "client_order_id": "client-v2-1",
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "intent": "ENTRY",
            "order_type": "MARKET",
            "qty": "1",
            "price": None,
            "leverage": 1,
            "signal_id": "signal-v2-1",
            "strategy_hash": "sha256:" + "a" * 64,
        },
    }
    client = FakeV2Client()
    result = OrderRecoveryProbe(
        client, FakeStore([record])
    ).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["halt_trading"] is True
    assert result["journal_mutated"] is False
    assert result["trading_state_mutated"] is False
    assert result["orders_submitted"] == 0
    assert result["orders_canceled"] == 0
    assert result["items"][0]["order_api_version"] == "V2"
    assert result["items"][0]["trades_api_version"] == "V2"
    assert [call[0] for call in client.calls] == [
        "order_v2", "trades_v2"
    ]


def test_probe_v2_full_page_is_not_declared_complete():
    record = {
        "client_order_id": "client-v2-1",
        "status": "SUBMISSION_STARTED",
        "created_at_ms": 1790000000000,
        "exchange_order_id": None,
        "intent": {
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "intent": "ENTRY",
            "qty": "1",
        },
    }

    class FullPageClient(FakeV2Client):
        def get_user_trades_v2(self, *args, **kwargs):
            return [v2_trade(id=f"trade-{i}") for i in range(10)]

    result = OrderRecoveryProbe(
        FullPageClient(), FakeStore([record]), trade_limit=10
    ).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["items"][0]["reason"] == (
        "user_trades_result_may_be_truncated"
    )