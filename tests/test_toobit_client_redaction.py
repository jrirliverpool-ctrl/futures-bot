
import os
import sys

import requests

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

from toobit_client import ToobitClient


def test_unknown_response_does_not_log_raw_exchange_payload(caplog):
    client = ToobitClient(
        api_key="TEST_KEY",
        api_secret="TEST_SECRET",
    )

    secret_marker = "PRIVATE_RESPONSE_SECRET_123"

    client._unknown_from(
        {
            "code": 500,
            "message": secret_marker,
            "accountData": secret_marker,
        },
        1.0,
        "simulated failure",
    )

    assert secret_marker not in caplog.text
    assert "simulated failure" in caplog.text


def test_request_exception_message_is_not_exposed(monkeypatch):
    client = ToobitClient(
        api_key="TEST_KEY",
        api_secret="TEST_SECRET",
    )

    secret_marker = "SIGNED_URL_SIGNATURE_SECRET"

    def fail_request(*args, **kwargs):
        raise requests.exceptions.ConnectionError(secret_marker)

    monkeypatch.setattr(
        "toobit_client.requests.get",
        fail_request,
    )

    result = client._request(
        "GET",
        "/api/v1/futures/balance",
        {},
    )

    assert result["_transport"] == "UNKNOWN"
    assert secret_marker not in result["error"]
    assert "ConnectionError" in result["error"]


def test_user_trades_error_does_not_expose_raw_body(
    monkeypatch,
):
    client = ToobitClient(
        api_key="TEST_KEY",
        api_secret="TEST_SECRET",
    )

    secret_marker = "PRIVATE_EXCHANGE_BODY_SECRET"

    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "_transport": "HTTP_ERROR",
            "_http_status": 400,
            "message": secret_marker,
        },
    )

    try:
        client.get_user_trades("BTC-SWAP-USDT")
        assert False, "RuntimeError expected"
    except RuntimeError as exc:
        assert secret_marker not in str(exc)
        assert "400" in str(exc)
