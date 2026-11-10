
import os
import sys

import pytest
import requests

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

from toobit_client import ToobitClient, VALID_STATUSES


@pytest.fixture
def client():
    return ToobitClient(
        api_key="TEST_KEY",
        api_secret="TEST_SECRET",
    )


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body

        return self.body


def test_valid_transitional_statuses():
    assert "PENDING_NEW" in VALID_STATUSES
    assert "PENDING_CANCEL" in VALID_STATUSES
    assert "PARTIALLY_FILLED" in VALID_STATUSES


def test_map_long_entry(client):
    assert client._map_side("LONG", "ENTRY") == "BUY_OPEN"


def test_map_long_exit(client):
    assert client._map_side("LONG", "EXIT") == "SELL_CLOSE"


def test_map_short_entry(client):
    assert client._map_side("SHORT", "ENTRY") == "SELL_OPEN"


def test_map_short_exit(client):
    assert client._map_side("SHORT", "EXIT") == "BUY_CLOSE"


def test_invalid_side_intent_rejected(client):
    with pytest.raises(ValueError):
        client._map_side("INVALID", "ENTRY")


def test_successful_order_parsing(client):
    raw = {
        "orderId": 12345,
        "clientOrderId": "apex_test_001",
        "symbol": "BTC-SWAP-USDT",
        "origQty": "1.0",
        "executedQty": "0.5",
        "avgPrice": "60000",
        "status": "PARTIALLY_FILLED",
    }

    result = client._parse_order(raw, 1.0)

    assert result.exchange_order_id == "12345"
    assert result.client_order_id == "apex_test_001"
    assert result.requested_qty == 1.0
    assert result.filled_qty == 0.5
    assert result.avg_price == 60000.0
    assert result.status == "PARTIALLY_FILLED"


def test_invalid_executed_qty_becomes_unknown(client):
    raw = {
        "orderId": 123,
        "origQty": "1",
        "executedQty": "invalid",
        "status": "NEW",
    }

    result = client._parse_order(raw, 1.0)

    assert result.status == "UNKNOWN"
    assert result.filled_qty == 0.0


def test_executed_qty_above_original_becomes_unknown(client):
    raw = {
        "orderId": 123,
        "origQty": "1",
        "executedQty": "2",
        "status": "FILLED",
    }

    result = client._parse_order(raw, 1.0)

    assert result.status == "UNKNOWN"


def test_unknown_status_is_not_accepted(client):
    raw = {
        "orderId": 123,
        "origQty": "1",
        "executedQty": "0",
        "status": "UNRECOGNIZED_STATUS",
    }

    result = client._parse_order(raw, 1.0)

    assert result.status == "UNKNOWN"


def test_http_500_is_unknown_and_not_success(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        "toobit_client.requests.post",
        lambda *args, **kwargs: FakeResponse(
            500,
            {"code": 500, "message": "server error"},
        ),
    )

    data = client._request(
        "POST",
        "/api/v1/futures/order",
        {"symbol": "BTC-SWAP-USDT"},
    )

    assert data["_transport"] == "UNKNOWN"
    assert client._is_success(data) is False


def test_http_400_is_not_success(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        "toobit_client.requests.post",
        lambda *args, **kwargs: FakeResponse(
            400,
            {"code": 400, "message": "bad request"},
        ),
    )

    data = client._request(
        "POST",
        "/api/v1/futures/order",
        {"symbol": "BTC-SWAP-USDT"},
    )

    assert data["_transport"] == "HTTP_ERROR"
    assert client._is_success(data) is False


def test_non_json_response_is_unknown(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        "toobit_client.requests.get",
        lambda *args, **kwargs: FakeResponse(
            200,
            ValueError("invalid JSON"),
        ),
    )

    data = client._request(
        "GET",
        "/api/v1/futures/order",
        {"symbol": "BTC-SWAP-USDT"},
    )

    assert data["_transport"] == "UNKNOWN"


def test_timeout_is_unknown(
    client,
    monkeypatch,
):
    def raise_timeout(*args, **kwargs):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(
        "toobit_client.requests.post",
        raise_timeout,
    )

    data = client._request(
        "POST",
        "/api/v1/futures/order",
        {"symbol": "BTC-SWAP-USDT"},
    )

    assert data["_transport"] == "UNKNOWN"
    assert "timeout" in data["error"]


def test_post_is_not_retried_after_timeout(
    client,
    monkeypatch,
):
    calls = {"count": 0}

    def raise_timeout(*args, **kwargs):
        calls["count"] += 1
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(
        "toobit_client.requests.post",
        raise_timeout,
    )

    client.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity=1.0,
        price=None,
        client_order_id="apex_test_timeout",
        intent="ENTRY",
    )

    assert calls["count"] == 1


@pytest.mark.parametrize(
    ("side", "intent", "expected_side", "expected_position_side"),
    [
        ("LONG", "ENTRY", "BUY", "LONG"),
        ("SHORT", "ENTRY", "SELL", "SHORT"),
        ("LONG", "EXIT", "SELL", "LONG"),
        ("SHORT", "EXIT", "BUY", "SHORT"),
    ],
)
def test_v2_order_maps_side_and_position_side(
    client, monkeypatch, side, intent, expected_side, expected_position_side
):
    calls = []

    def fake_post(path, params):
        calls.append((path, params))
        return {
            "orderId": "123",
            "clientOrderId": "apex_v2_test",
            "symbol": "BTC-SWAP-USDT",
            "origQty": "0.01",
            "executedQty": "0",
            "avgPrice": "0",
            "status": "NEW",
            "side": expected_side,
            "positionSide": expected_position_side,
        }

    monkeypatch.setattr(client, "_request_v2_post", fake_post)
    result = client.place_order_v2(
        symbol="BTC-SWAP-USDT",
        side=side,
        order_type="MARKET",
        quantity="0.01",
        price=None,
        client_order_id="apex_v2_test",
        intent=intent,
    )

    assert result.status == "NEW"
    assert result.exchange_order_id == "123"
    assert len(calls) == 1
    path, params = calls[0]
    assert path == "/api/v2/futures/order"
    assert params["side"] == expected_side
    assert params["positionSide"] == expected_position_side
    assert params["category"] == "USDT"
    assert "price" not in params


def test_v2_limit_order_includes_price_and_time_in_force(client, monkeypatch):
    captured = {}

    def fake_post(path, params):
        captured.update(path=path, params=params)
        return {
            "orderId": "124",
            "clientOrderId": "apex_v2_limit",
            "symbol": "BTC-SWAP-USDT",
            "origQty": "0.01",
            "executedQty": "0",
            "avgPrice": "0",
            "status": "NEW",
            "side": "BUY",
            "positionSide": "LONG",
        }

    monkeypatch.setattr(client, "_request_v2_post", fake_post)
    result = client.place_order_v2(
        "BTC-SWAP-USDT", "LONG", "LIMIT", "0.01", "60000",
        "apex_v2_limit", "ENTRY", time_in_force="POST_ONLY"
    )

    assert result.status == "NEW"
    assert captured["params"]["price"] == "60000"
    assert captured["params"]["timeInForce"] == "POST_ONLY"


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": 0},
        {"quantity": float("nan")},
        {"quantity": True},
        {"order_type": "STOP"},
        {"order_type": "LIMIT", "price": None},
        {"order_type": "MARKET", "price": 1},
        {"time_in_force": "INVALID"},
    ],
)
def test_v2_invalid_order_does_not_send_request(client, monkeypatch, overrides):
    calls = {"count": 0}

    def fake_post(path, params):
        calls["count"] += 1
        raise AssertionError("invalid order must not be sent")

    monkeypatch.setattr(client, "_request_v2_post", fake_post)
    args = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "order_type": "MARKET",
        "quantity": "0.01",
        "price": None,
        "client_order_id": "apex_v2_invalid",
        "intent": "ENTRY",
    }
    args.update(overrides)
    result = client.place_order_v2(**args)

    assert result.status == "UNKNOWN"
    assert calls["count"] == 0


def test_v2_ambiguous_submission_is_not_retried(client, monkeypatch):
    calls = {"count": 0}

    def ambiguous_post(path, params):
        calls["count"] += 1
        raise RuntimeError("V2 POST outcome is unknown (Timeout)")

    monkeypatch.setattr(client, "_request_v2_post", ambiguous_post)
    result = client.place_order_v2(
        "BTC-SWAP-USDT", "SHORT", "MARKET", "0.01", None,
        "apex_v2_unknown", "ENTRY"
    )

    assert result.status == "UNKNOWN"
    assert calls["count"] == 1


def test_v2_real_post_serializes_order_and_signature_once(client, monkeypatch):
    captured = {}

    def fake_requests_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(
            200,
            {
                "code": 200,
                "msg": "success",
                "data": {
                    "orderId": "125",
                    "clientOrderId": "apex_signed_v2",
                    "symbol": "BTC-SWAP-USDT",
                    "origQty": "0.01",
                    "executedQty": "0",
                    "avgPrice": "0",
                    "status": "NEW",
                    "side": "SELL",
                    "positionSide": "SHORT",
                },
            },
        )

    monkeypatch.setattr("toobit_client.requests.post", fake_requests_post)
    result = client.place_order_v2(
        "BTC-SWAP-USDT", "SHORT", "MARKET", "0.01", None,
        "apex_signed_v2", "ENTRY"
    )

    assert result.status == "NEW"
    assert captured["url"].endswith("/api/v2/futures/order")
    assert captured["headers"] == {"X-BB-APIKEY": "TEST_KEY"}
    assert captured["timeout"] == 15
    sent = dict(captured["data"])
    assert sent["symbol"] == "BTC-SWAP-USDT"
    assert sent["side"] == "SELL"
    assert sent["positionSide"] == "SHORT"
    assert sent["type"] == "MARKET"
    assert sent["newClientOrderId"] == "apex_signed_v2"
    assert sent["category"] == "USDT"
    assert sent["recvWindow"] == 5000
    assert sent["signature"] == client._generate_signature(
        {key: value for key, value in sent.items() if key != "signature"}
    )


def test_limit_order_requires_price(client):
    result = client.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="LIMIT",
        quantity=1.0,
        price=None,
        client_order_id="apex_test_limit",
        intent="ENTRY",
    )

    assert result.status == "UNKNOWN"


def test_invalid_order_type_does_not_send_request(
    client,
    monkeypatch,
):
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        return FakeResponse(200, {})

    monkeypatch.setattr(
        "toobit_client.requests.post",
        fake_post,
    )

    result = client.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="INVALID",
        quantity=1.0,
        price=None,
        client_order_id="apex_test_invalid",
        intent="ENTRY",
    )

    assert result.status == "UNKNOWN"
    assert calls["count"] == 0
