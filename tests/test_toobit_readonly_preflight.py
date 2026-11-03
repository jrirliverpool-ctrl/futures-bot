import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from toobit_readonly_preflight import build_snapshot, _as_list, public_summary


class FakeClient:
    def __init__(self, overrides=None):
        self.calls = []
        self.responses = {
            "/api/v1/futures/balance": [
                {"coin": "USDT", "balance": "100", "availableBalance": "95",
                 "positionMargin": "0", "orderMargin": "0", "crossUnRealizedPnl": "-0.25"}
            ],
            "/api/v1/futures/positions": [],
            "/api/v1/futures/openOrders": [],
            "/api/v1/futures/accountLeverage": [
                {"symbolId": "BTC-SWAP-USDT", "leverage": "3", "marginType": "ISOLATED"}
            ],
        }
        self.responses.update(overrides or {})

    def _request(self, method, path, params):
        self.calls.append((method, path, params))
        return self.responses[path]


class ReadOnlyPreflightTests(unittest.TestCase):
    def test_reads_only_documented_get_endpoints(self):
        client = FakeClient()
        report = build_snapshot(client)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["orders_submitted"], 0)
        self.assertEqual(report["orders_amended"], 0)
        self.assertEqual(report["orders_canceled"], 0)
        self.assertFalse(report["halt_live"])
        self.assertEqual(report["wallet"]["crossUnRealizedPnl"], "-0.25")
        self.assertEqual(
            client.calls,
            [
                ("GET", "/api/v1/futures/balance", {}),
                ("GET", "/api/v1/futures/positions", {}),
                ("GET", "/api/v1/futures/openOrders", {"limit": 1000}),
                ("GET", "/api/v1/futures/accountLeverage", {}),
            ],
        )
        self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_active_position_blocks_live(self):
        client = FakeClient({"/api/v1/futures/positions": [
            {"symbol": "BTC-SWAP-USDT", "side": "SHORT", "position": "0.1"}
        ]})
        report = build_snapshot(client)
        self.assertTrue(report["halt_live"])
        self.assertIn("open_positions_exist", report["blockers"])

    def test_open_conditional_order_blocks_live(self):
        client = FakeClient({"/api/v1/futures/openOrders": [
            {"orderId": "123", "symbol": "BTC-SWAP-USDT", "side": "BUY_CLOSE",
             "status": "ORDER_NEW", "type": "STOP_PROFIT_LOSS"}
        ]})
        report = build_snapshot(client)
        self.assertTrue(report["halt_live"])
        self.assertIn("open_orders_exist", report["blockers"])

    def test_zero_position_is_not_active(self):
        client = FakeClient({"/api/v1/futures/positions": [
            {"symbol": "BTC-SWAP-USDT", "side": "LONG", "position": "0"}
        ]})
        report = build_snapshot(client)
        self.assertFalse(report["halt_live"])

    def test_api_transport_error_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _as_list({"_transport": "UNKNOWN", "error": "timeout"}, "positions")

    def test_exchange_error_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _as_list({"code": -1000, "msg": "failure"}, "positions")

    def test_unexpected_dict_does_not_become_empty_list(self):
        with self.assertRaises(RuntimeError):
            _as_list({"unexpected": []}, "positions")

    def test_malformed_position_fails_closed(self):
        client = FakeClient({"/api/v1/futures/positions": [
            {"symbol": "BTC-SWAP-USDT", "side": "BOTH", "position": "1"}
        ]})
        with self.assertRaises(RuntimeError):
            build_snapshot(client)

    def test_malformed_open_order_fails_closed(self):
        client = FakeClient({"/api/v1/futures/openOrders": [
            {"orderId": "123", "symbol": "BTC-SWAP-USDT", "side": "BUY_CLOSE"}
        ]})
        with self.assertRaises(RuntimeError):
            build_snapshot(client)

    def test_missing_usdt_wallet_fails_closed(self):
        client = FakeClient({"/api/v1/futures/balance": [
            {"coin": "BTC", "balance": "1"}
        ]})
        with self.assertRaises(RuntimeError):
            build_snapshot(client)


    def test_public_summary_redacts_account_values(self):
        report = build_snapshot(FakeClient({
            "/api/v1/futures/balance": [{"coin": "USDT", "balance": "9876.54", "availableBalance": "9000"}],
            "/api/v1/futures/positions": [{
                "symbol": "BTC-SWAP-USDT", "side": "LONG", "position": "12.5"
            }],
        }))
        summary = public_summary(report)
        serialized = str(summary)
        self.assertTrue(summary["account_values_redacted"])
        self.assertNotIn("9876.54", serialized)
        self.assertNotIn("9000", serialized)
        self.assertNotIn("12.5", serialized)
        self.assertNotIn("BTC-SWAP-USDT", serialized)
        self.assertEqual(summary["active_positions_count"], 1)
        self.assertTrue(summary["halt_live"])

    def test_bad_leverage_fails_closed(self):
        client = FakeClient({"/api/v1/futures/accountLeverage": [
            {"symbolId": "BTC-SWAP-USDT", "leverage": "NaN", "marginType": "ISOLATED"}
        ]})
        with self.assertRaises(RuntimeError):
            build_snapshot(client)


if __name__ == "__main__":
    unittest.main()