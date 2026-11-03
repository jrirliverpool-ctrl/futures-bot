import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(SCRIPTS))

from toobit_readonly_preflight import (
    ENDPOINTS,
    PreflightError,
    _as_list,
    build_snapshot,
    load_symbols,
    public_summary,
)


SYMBOLS = [
    "BTC-SWAP-USDT",
    "ETH-SWAP-USDT",
    "SOL-SWAP-USDT",
    "BNB-SWAP-USDT",
    "XRP-SWAP-USDT",
    "ADA-SWAP-USDT",
    "DOGE-SWAP-USDT",
    "AVAX-SWAP-USDT",
    "LINK-SWAP-USDT",
    "DOT-SWAP-USDT",
]


class FakeClient:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

        self.responses = {
            ENDPOINTS["balance"]: [
                {
                    "coin": "USDT",
                    "balance": "100",
                    "availableBalance": "95",
                    "positionMargin": "0",
                    "orderMargin": "0",
                    "crossUnRealizedPnl": "-0.25",
                }
            ],
            ENDPOINTS["positions"]: [],
            ENDPOINTS["open_orders"]: [],
        }

        self.responses.update({
            key: value
            for key, value in self.overrides.items()
            if key in self.responses
        })

    def _request(self, method, path, params):
        self.calls.append((method, path, dict(params)))

        if path == ENDPOINTS["leverage"]:
            symbol = params.get("symbol")

            override = self.overrides.get(
                ("leverage", symbol)
            )

            if override is not None:
                if isinstance(override, Exception):
                    raise override
                return override

            return [
                {
                    "symbolId": symbol,
                    "leverage": "3",
                    "marginType": "ISOLATED",
                }
            ]

        return self.responses[path]


class ReadOnlyPreflightTests(unittest.TestCase):

    def test_reads_only_get_endpoints(self):
        client = FakeClient()
        report = build_snapshot(client)

        self.assertTrue(report["read_only"])
        self.assertEqual(report["orders_submitted"], 0)
        self.assertEqual(report["orders_amended"], 0)
        self.assertEqual(report["orders_canceled"], 0)
        self.assertFalse(report["halt_live"])

        self.assertEqual(
            report["wallet"]["crossUnRealizedPnl"],
            "-0.25",
        )

        self.assertTrue(
            all(call[0] == "GET" for call in client.calls)
        )

        self.assertEqual(
            client.calls[:3],
            [
                ("GET", ENDPOINTS["balance"], {}),
                ("GET", ENDPOINTS["positions"], {}),
                ("GET", ENDPOINTS["open_orders"], {"limit": 1000}),
            ],
        )

    def test_leverage_request_contains_symbol_parameter(self):
        client = FakeClient()
        build_snapshot(client)

        leverage_calls = [
            call for call in client.calls
            if call[1] == ENDPOINTS["leverage"]
        ]

        self.assertEqual(len(leverage_calls), len(SYMBOLS))

        for call, symbol in zip(leverage_calls, SYMBOLS):
            self.assertEqual(call[0], "GET")
            self.assertEqual(call[2], {"symbol": symbol})

    def test_all_whitelist_symbols_have_matching_leverage_rows(self):
        client = FakeClient()
        report = build_snapshot(client)

        self.assertEqual(
            report["symbols_checked"],
            SYMBOLS,
        )

        self.assertEqual(
            len(report["leverage_settings"]),
            len(SYMBOLS),
        )

        returned_symbols = [
            row["symbolId"]
            for row in report["leverage_settings"]
        ]

        self.assertEqual(returned_symbols, SYMBOLS)

    def test_active_position_blocks_live(self):
        client = FakeClient({
            ENDPOINTS["positions"]: [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "SHORT",
                    "position": "0.1",
                }
            ]
        })

        report = build_snapshot(client)

        self.assertTrue(report["halt_live"])
        self.assertIn(
            "open_positions_exist",
            report["blockers"],
        )

    def test_open_conditional_order_blocks_live(self):
        client = FakeClient({
            ENDPOINTS["open_orders"]: [
                {
                    "orderId": "123",
                    "symbol": "BTC-SWAP-USDT",
                    "side": "BUY_CLOSE",
                    "status": "ORDER_NEW",
                    "type": "STOP_PROFIT_LOSS",
                }
            ]
        })

        report = build_snapshot(client)

        self.assertTrue(report["halt_live"])
        self.assertIn(
            "open_orders_exist",
            report["blockers"],
        )

    def test_zero_position_is_not_active(self):
        client = FakeClient({
            ENDPOINTS["positions"]: [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "LONG",
                    "position": "0",
                }
            ]
        })

        report = build_snapshot(client)

        self.assertFalse(report["halt_live"])

    def test_transport_error_fails_closed(self):
        with self.assertRaises(PreflightError):
            _as_list(
                {
                    "_transport": "UNKNOWN",
                    "error": "timeout",
                },
                "positions",
            )

    def test_exchange_error_fails_closed(self):
        with self.assertRaises(PreflightError):
            _as_list(
                {
                    "code": -1000,
                    "msg": "failure",
                },
                "positions",
            )

    def test_unexpected_dict_does_not_become_empty_list(self):
        with self.assertRaises(PreflightError):
            _as_list(
                {"unexpected": []},
                "positions",
            )

    def test_malformed_position_fails_closed(self):
        client = FakeClient({
            ENDPOINTS["positions"]: [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "BOTH",
                    "position": "1",
                }
            ]
        })

        with self.assertRaises(PreflightError):
            build_snapshot(client)

    def test_malformed_open_order_fails_closed(self):
        client = FakeClient({
            ENDPOINTS["open_orders"]: [
                {
                    "orderId": "123",
                    "symbol": "BTC-SWAP-USDT",
                    "side": "BUY_CLOSE",
                }
            ]
        })

        with self.assertRaises(PreflightError):
            build_snapshot(client)

    def test_missing_usdt_wallet_fails_closed(self):
        client = FakeClient({
            ENDPOINTS["balance"]: [
                {
                    "coin": "BTC",
                    "balance": "1",
                }
            ]
        })

        with self.assertRaises(PreflightError):
            build_snapshot(client)

    def test_bad_leverage_fails_closed(self):
        client = FakeClient({
            ("leverage", SYMBOLS[0]): [
                {
                    "symbolId": SYMBOLS[0],
                    "leverage": "NaN",
                    "marginType": "ISOLATED",
                }
            ]
        })

        with self.assertRaises(PreflightError):
            build_snapshot(client)

    def test_missing_leverage_row_fails_closed(self):
        client = FakeClient({
            ("leverage", SYMBOLS[0]): []
        })

        with self.assertRaises(PreflightError):
            build_snapshot(client)

    def test_mismatched_leverage_symbol_fails_closed(self):
        client = FakeClient({
            ("leverage", SYMBOLS[0]): [
                {
                    "symbolId": "ETH-SWAP-USDT",
                    "leverage": "3",
                    "marginType": "ISOLATED",
                }
            ]
        })

        with self.assertRaises(PreflightError):
            build_snapshot(client)

    def test_public_summary_redacts_account_values(self):
        client = FakeClient({
            ENDPOINTS["balance"]: [
                {
                    "coin": "USDT",
                    "balance": "9876.54",
                    "availableBalance": "9000",
                }
            ],
            ENDPOINTS["positions"]: [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "LONG",
                    "position": "12.5",
                }
            ],
        })

        report = build_snapshot(client)
        summary = public_summary(report)
        serialized = json.dumps(summary)

        self.assertTrue(summary["account_values_redacted"])
        self.assertNotIn("9876.54", serialized)
        self.assertNotIn("9000", serialized)
        self.assertNotIn("12.5", serialized)
        self.assertNotIn("BTC-SWAP-USDT", serialized)
        self.assertEqual(summary["active_positions_count"], 1)
        self.assertTrue(summary["halt_live"])

    def test_whitelist_rejects_duplicate_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "whitelist.json"
            path.write_text(
                json.dumps({
                    "symbols": [
                        "BTC-SWAP-USDT",
                        "BTC-SWAP-USDT",
                    ]
                }),
                encoding="utf-8",
            )

            with self.assertRaises(PreflightError):
                load_symbols(path)

    def test_whitelist_rejects_invalid_symbol_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "whitelist.json"
            path.write_text(
                json.dumps({
                    "symbols": ["BTC-USDT"]
                }),
                encoding="utf-8",
            )

            with self.assertRaises(PreflightError):
                load_symbols(path)


if __name__ == "__main__":
    unittest.main()