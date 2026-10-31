
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
