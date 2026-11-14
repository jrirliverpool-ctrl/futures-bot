import hashlib
import hmac
import json
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlparse, urlencode

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toobit_v2_order_adapter import ToobitV2OrderAdapter


class FakeClient:
    api_key = "TEST_KEY"
    api_secret = "TEST_SECRET"

    def _v2_order_side(self, data):
        return {
            ("BUY", "LONG"): "BUY_OPEN",
            ("SELL", "LONG"): "SELL_CLOSE",
            ("SELL", "SHORT"): "SELL_OPEN",
            ("BUY", "SHORT"): "BUY_CLOSE",
        }.get((data.get("side"), data.get("positionSide")))

    def _unknown_from(self, data, requested_qty, reason):
        from dataclasses import dataclass
        @dataclass
        class Response:
            exchange_order_id: object
            client_order_id: object
            symbol: str
            requested_qty: float
            filled_qty: float
            avg_price: object
            status: str
            raw_response: dict
        return Response(None, None, "", requested_qty, 0.0, None, "UNKNOWN", data)

    def _parse_order(self, raw_data, requested_qty):
        data = raw_data.get("data", {})
        from dataclasses import dataclass
        @dataclass
        class Response:
            exchange_order_id: object
            client_order_id: object
            symbol: str
            requested_qty: float
            filled_qty: float
            avg_price: object
            status: str
            raw_response: dict
        return Response(
            str(data.get("orderId")) if data.get("orderId") is not None else None,
            data.get("clientOrderId"),
            data.get("symbol", ""),
            float(data.get("origQty", requested_qty)),
            float(data.get("executedQty", 0)),
            float(data.get("avgPrice", 0)),
            data.get("status", "UNKNOWN"),
            raw_data,
        )


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.fixture
def adapter():
    return ToobitV2OrderAdapter(FakeClient(), allow_network_submission=True)


def order_data(**overrides):
    result = {
        "time": "1790000000000",
        "updateTime": "1790000000000",
        "orderId": "90000001",
        "clientOrderId": "aq_test_v2_01",
        "symbol": "BTC-SWAP-USDT",
        "price": "0",
        "leverage": "3",
        "origQty": "0.1",
        "executedQty": "0",
        "avgPrice": "0",
        "type": "MARKET",
        "side": "BUY",
        "positionSide": "LONG",
        "status": "NEW",
        "contractMultiplier": "0.001",
    }
    result.update(overrides)
    return result


def test_market_order_sends_decimal_contract_qty_and_valid_signature(adapter, monkeypatch):
    captured = {}

    def fake_post(url, headers, data, timeout):
        captured.update(url=url, headers=headers, data=data, timeout=timeout)
        return FakeResponse(200, {"code": 200, "msg": "success", "data": order_data()})

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)

    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_test_v2_01",
        intent="ENTRY",
    )

    assert result.status == "NEW"
    assert result.requested_qty == pytest.approx(0.1)
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["X-BB-APIKEY"] == "TEST_KEY"
    body = json.loads(captured["data"])
    assert body["quantity"] == "0.1"
    assert body["side"] == "BUY"
    assert body["positionSide"] == "LONG"
    assert body["type"] == "MARKET"
    assert "price" not in body

    parsed = urlparse(captured["url"])
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query_items if key != "signature"]
    params = dict(query_items)
    actual_signature = params["signature"]
    query_string = urlencode(query)
    signed = query_string + captured["data"]
    expected_signature = hmac.new(
        b"TEST_SECRET", signed.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert actual_signature == expected_signature
    assert params["category"] == "USDT"
    assert params["recvWindow"] == "5000"


def test_limit_order_includes_price_and_time_in_force(adapter, monkeypatch):
    captured = {}

    def fake_post(url, headers, data, timeout):
        captured["body"] = json.loads(data)
        return FakeResponse(200, {
            "code": 200,
            "data": order_data(
                type="LIMIT",
                price="67000.5",
                side="SELL",
                positionSide="SHORT",
                clientOrderId="aq_short_limit_1",
                origQty="0.1",
            ),
        })

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)
    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="SHORT",
        order_type="LIMIT",
        quantity=Decimal("0.1"),
        price=Decimal("67000.5"),
        client_order_id="aq_short_limit_1",
        intent="ENTRY",
        time_in_force="IOC",
    )

    assert result.status == "NEW"
    assert captured["body"]["side"] == "SELL"
    assert captured["body"]["positionSide"] == "SHORT"
    assert captured["body"]["price"] == "67000.5"
    assert captured["body"]["timeInForce"] == "IOC"


@pytest.mark.parametrize(
    "side,intent,expected_side,expected_position_side",
    [
        ("LONG", "ENTRY", "BUY", "LONG"),
        ("LONG", "EXIT", "SELL", "LONG"),
        ("SHORT", "ENTRY", "SELL", "SHORT"),
        ("SHORT", "EXIT", "BUY", "SHORT"),
        ("LONG", "TP1_HIT", "SELL", "LONG"),
        ("SHORT", "TP1_HIT", "BUY", "SHORT"),
    ],
)
def test_all_side_and_intent_mappings(adapter, monkeypatch, side, intent, expected_side, expected_position_side):
    captured = {}

    def fake_post(url, headers, data, timeout):
        captured["body"] = json.loads(data)
        data = order_data(
            side=expected_side,
            positionSide=expected_position_side,
            clientOrderId="aq_map_test",
        )
        return FakeResponse(200, {"code": 200, "data": data})

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)
    adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side=side,
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_map_test",
        intent=intent,
    )
    assert captured["body"]["side"] == expected_side
    assert captured["body"]["positionSide"] == expected_position_side


def test_response_identity_mismatch_returns_unknown(adapter, monkeypatch):
    monkeypatch.setattr(
        "toobit_v2_order_adapter.requests.post",
        lambda *a, **k: FakeResponse(200, {
            "code": 200,
            "data": order_data(clientOrderId="another_order"),
        }),
    )
    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_test_v2_01",
        intent="ENTRY",
    )
    assert result.status == "UNKNOWN"


def test_response_quantity_mismatch_returns_unknown(adapter, monkeypatch):
    monkeypatch.setattr(
        "toobit_v2_order_adapter.requests.post",
        lambda *a, **k: FakeResponse(200, {
            "code": 200,
            "data": order_data(origQty="1"),
        }),
    )
    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_test_v2_01",
        intent="ENTRY",
    )
    assert result.status == "UNKNOWN"


def test_http_error_returns_unknown_without_retry(adapter, monkeypatch):
    calls = {"count": 0}

    def fake_post(*a, **k):
        calls["count"] += 1
        return FakeResponse(502, {"code": -1, "msg": "bad gateway"})

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)
    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_test_v2_01",
        intent="ENTRY",
    )
    assert result.status == "UNKNOWN"
    assert calls["count"] == 1


def test_timeout_returns_unknown_without_retry(adapter, monkeypatch):
    calls = {"count": 0}

    def fake_post(*a, **k):
        calls["count"] += 1
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)
    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_test_v2_01",
        intent="ENTRY",
    )
    assert result.status == "UNKNOWN"
    assert calls["count"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": "0"},
        {"quantity": "NaN"},
        {"quantity": "Infinity"},
        {"quantity": True},
        {"side": "INVALID"},
        {"intent": "UNKNOWN"},
        {"order_type": "STOP"},
        {"price": "10"},
    ],
)
def test_invalid_order_shape_is_rejected_before_network(adapter, monkeypatch, overrides):
    calls = {"count": 0}

    def fake_post(*a, **k):
        calls["count"] += 1
        return FakeResponse(200, {"code": 200, "data": order_data()})

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)
    kwargs = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "order_type": "MARKET",
        "quantity": "0.1",
        "price": None,
        "client_order_id": "aq_test_v2_01",
        "intent": "ENTRY",
    }
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        adapter.place_order(**kwargs)
    assert calls["count"] == 0


def test_api_error_code_returns_unknown(adapter, monkeypatch):
    monkeypatch.setattr(
        "toobit_v2_order_adapter.requests.post",
        lambda *a, **k: FakeResponse(200, {"code": -2011, "msg": "rejected", "data": None}),
    )
    result = adapter.place_order(
        symbol="BTC-SWAP-USDT",
        side="LONG",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        client_order_id="aq_test_v2_01",
        intent="ENTRY",
    )
    assert result.status == "UNKNOWN"


def test_network_submission_is_disabled_by_default(monkeypatch):
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("default-locked adapter must not send HTTP")

    monkeypatch.setattr("toobit_v2_order_adapter.requests.post", fake_post)
    adapter = ToobitV2OrderAdapter(FakeClient())
    with pytest.raises(PermissionError, match="explicit opt-in"):
        adapter.place_order(
            symbol="BTC-SWAP-USDT",
            side="LONG",
            order_type="MARKET",
            quantity="0.1",
            price=None,
            client_order_id="aq_test_default_locked",
            intent="ENTRY",
        )
    assert calls["count"] == 0


def test_non_boolean_network_submission_flag_is_rejected():
    with pytest.raises(ValueError, match="must be bool"):
        ToobitV2OrderAdapter(FakeClient(), allow_network_submission="true")