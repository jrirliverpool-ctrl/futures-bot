"""
test_toobit_client.py — Offline security and behavior tests.
"""
import hashlib
import hmac
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
import requests

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from toobit_client import ToobitClient

def make_client():
    return ToobitClient("fake_key", "fake_secret")

def mock_response(status_code=200, json_data=None, json_error=None):
    response = MagicMock()
    response.status_code = status_code

    if json_error:
        response.json.side_effect = ValueError("Invalid JSON")
    else:
        response.json.return_value = json_data

    return response

def test_signature_matches_known_hmac_sha256():
    client = make_client()
    params = {
        "symbol": "BTCUSDT",
        "timestamp": 1600000000000,
        "recvWindow": 5000,
    }

    query = urlencode(sorted(params.items()))
    actual = client._generate_signature(query)

    expected = hmac.new(
        b"fake_secret",
        b"recvWindow=5000&symbol=BTCUSDT&timestamp=1600000000000",
        hashlib.sha256,
    ).hexdigest()

    assert actual == expected
    assert len(actual) == 64

@patch("requests.Session.get")
def test_successful_balance_dict(mock_get):
    mock_get.return_value = mock_response(
        json_data={"code": 200, "msg": "success", "data": {"USDT": "1000"}}
    )

    result = make_client().get_balance(timestamp=1600000000000)

    assert result["status"] == "SUCCESS"
    assert result["data"]["data"]["USDT"] == "1000"

@patch("requests.Session.get")
def test_successful_balance_array(mock_get):
    mock_get.return_value = mock_response(
        json_data=[{"asset": "USDT", "balance": "1000"}]
    )

    result = make_client().get_balance(timestamp=1600000000000)

    assert result["status"] == "SUCCESS"
    assert result["data"][0]["asset"] == "USDT"

@patch("requests.Session.get")
def test_http_401_does_not_expose_response_body(mock_get):
    response = mock_response(status_code=401)
    response.text = "Sensitive server response"
    mock_get.return_value = response

    result = make_client().get_balance(timestamp=1600000000000)

    assert result == {"status": "HTTP_ERROR", "status_code": 401}
    assert "raw" not in result

@patch("requests.Session.get")
def test_api_error_returns_code_without_raw_message(mock_get):
    mock_get.return_value = mock_response(
        json_data={"code": -1001, "msg": "Invalid API-key"}
    )

    result = make_client().get_balance(timestamp=1600000000000)

    assert result["status"] == "API_ERROR"
    assert result["api_code"] == "-1001"
    assert "error" not in result
    assert "raw" not in result

@patch("requests.Session.get")
def test_network_timeout(mock_get):
    mock_get.side_effect = requests.exceptions.Timeout()

    result = make_client().get_balance(timestamp=1600000000000)

    assert result == {
        "status": "NETWORK_ERROR",
        "error": "TIMEOUT",
    }

@patch("requests.Session.get")
def test_connection_error_is_sanitized(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError(
        "sensitive connection details"
    )

    result = make_client().get_balance(timestamp=1600000000000)

    assert result == {
        "status": "NETWORK_ERROR",
        "error": "REQUEST_FAILED",
    }

@patch("requests.Session.get")
def test_invalid_json_is_rejected(mock_get):
    mock_get.return_value = mock_response(json_error=True)

    result = make_client().get_balance(timestamp=1600000000000)

    assert result == {
        "status": "INVALID_RESPONSE",
        "error": "INVALID_JSON",
    }

@patch("requests.Session.get")
def test_post_is_rejected_without_network_call(mock_get):
    client = make_client()

    result = client._request(
        "POST",
        client.BALANCE_ENDPOINT,
        timestamp=1600000000000,
    )

    assert result["status"] == "REJECTED"
    mock_get.assert_not_called()

@patch("requests.Session.get")
def test_unknown_endpoint_is_rejected(mock_get):
    client = make_client()

    result = client._request(
        "GET",
        "/api/v1/futures/order",
        timestamp=1600000000000,
    )

    assert result["status"] == "REJECTED"
    mock_get.assert_not_called()

@patch("requests.Session.get")
def test_boolean_timestamp_is_rejected(mock_get):
    result = make_client().get_balance(timestamp=True)

    assert result["status"] == "INVALID_REQUEST"
    mock_get.assert_not_called()

@patch("requests.Session.get")
def test_caller_cannot_override_security_parameters(mock_get):
    client = make_client()

    result = client._request(
        "GET",
        client.BALANCE_ENDPOINT,
        params={"timestamp": 123},
    )

    assert result["status"] == "INVALID_REQUEST"
    mock_get.assert_not_called()

if __name__ == "__main__":
    pytest.main([__file__, "-v"])