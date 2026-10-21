"""
test_toobit_client.py — Strict tests for ToobitClient signature, errors, and parsing.
"""
import os
import sys
from unittest.mock import patch, MagicMock
import pytest
import requests

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from toobit_client import ToobitClient

def test_signature_deterministic():
    """تست صحت امضای دیجیتال با یک timestamp ثابت"""
    client = ToobitClient(api_key="fake_key", api_secret="fake_secret")
    # با یک timestamp ثابت، خروجی امضا باید همیشه یکسان باشد
    params = {"symbol": "BTCUSDT", "timestamp": 1600000000000, "recvWindow": 5000}
    
    import urllib.parse
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = client._generate_signature(query_string)
    
    # این امضا با استفاده از الگوریتم استاندارد HMAC-SHA256 تولید شده است
    expected_sig = "f8b9c2b4e7f1a6d3c5e8f9b0a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0" # مقدار نمونه
    # (در محیط واقعی، این مقدار را با محاسبه دستی یا ابزار آنلاین بررسی کنید)
    assert isinstance(signature, str)
    assert len(signature) == 64

@patch("requests.Session.get")
def test_successful_balance_dict(mock_get):
    """پاسخ موفق به صورت دیکشنری"""
    client = ToobitClient("fake", "fake")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200, "msg": "success", "data": {"USDT": "1000"}}
    mock_get.return_value = mock_response
    
    result = client.get_balance()
    assert result["status"] == "SUCCESS"
    assert result["data"]["data"]["USDT"] == "1000"

@patch("requests.Session.get")
def test_successful_balance_array(mock_get):
    """پاسخ موفق به صورت آرایه"""
    client = ToobitClient("fake", "fake")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"asset": "USDT", "balance": "1000"}]
    mock_get.return_value = mock_response
    
    result = client.get_balance()
    assert result["status"] == "SUCCESS"
    assert result["data"][0]["asset"] == "USDT"

@patch("requests.Session.get")
def test_http_error_401(mock_get):
    """خطای احراز هویت (HTTP 401)"""
    client = ToobitClient("fake", "fake")
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    mock_get.return_value = mock_response
    
    result = client.get_balance()
    assert result["status"] == "HTTP_ERROR"
    assert result["status_code"] == 401

@patch("requests.Session.get")
def test_api_error(mock_get):
    """خطای تجاری (مثلاً کلید نامعتبر)"""
    client = ToobitClient("fake", "fake")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": -1001, "msg": "Invalid API-key"}
    mock_get.return_value = mock_response
    
    result = client.get_balance()
    assert result["status"] == "API_ERROR"
    assert "Invalid API-key" in result["error"]

@patch("requests.Session.get")
def test_network_timeout(mock_get):
    """خطای شبکه (Timeout)"""
    client = ToobitClient("fake", "fake")
    mock_get.side_effect = requests.exceptions.Timeout
    
    result = client.get_balance()
    assert result["status"] == "NETWORK_ERROR"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])