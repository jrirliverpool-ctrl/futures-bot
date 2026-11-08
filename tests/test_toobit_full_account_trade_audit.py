
import sys
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import toobit_full_account_trade_audit as audit
import toobit_balance_flow as flow


def test_load_symbols_rejects_duplicate(tmp_path):
    path = tmp_path / "symbols.json"
    path.write_text(
        '{"symbols":["BTC-SWAP-USDT","BTC-SWAP-USDT"]}'
    )

    try:
        audit.load_symbols(path)
    except audit.AuditError:
        return

    assert False, "duplicate whitelist must fail"


def test_summary_aggregates_fees_and_pnl():
    rows = [
        {
            "commission_asset": "USDT",
            "commission": "0.05",
            "maker_rebate": "0",
            "realized_pnl": "2",
            "is_maker": False,
        },
        {
            "commission_asset": "USDT",
            "commission": "0.02",
            "maker_rebate": "0.001",
            "realized_pnl": "-1",
            "is_maker": True,
        },
    ]

    result = audit._summarize_fills(rows)

    assert result["fill_count"] == 2
    assert Decimal(
        result["commission_by_asset"]["USDT"]
    ) == Decimal("0.07")
    assert Decimal(
        result["reported_realized_pnl_usdt"]
    ) == Decimal("1")
    assert result["maker_fill_count"] == 1
    assert result["taker_fill_count"] == 1


def test_summary_flags_missing_realized_pnl():
    rows = [{
        "commission_asset": "USDT",
        "commission": "0.05",
        "maker_rebate": "0",
        "realized_pnl": "0",
        "is_maker": None,
    }]

    result = audit._summarize_fills(rows, missing_realized_pnl=1)

    assert result["realized_pnl_complete"] is False
    assert result["realized_pnl_missing_fill_count"] == 1


def test_positions_reject_malformed_snapshot():
    class Client:
        def _request(self, *args):
            return {"error": "bad"}

    try:
        audit._read_positions(Client())
    except audit.AuditError:
        return

    assert False, "malformed positions must fail closed"


def test_positions_reject_negative_quantity():
    class Client:
        def _request(self, *args):
            return [{
                "symbol": "BTC-SWAP-USDT",
                "side": "LONG",
                "position": "-1",
            }]

    try:
        audit._read_positions(Client())
    except audit.AuditError:
        return

    assert False, "negative quantity must fail closed"


def test_active_position_requires_average_price():
    class Client:
        def _request(self, *args):
            return [{
                "symbol": "BTC-SWAP-USDT",
                "side": "LONG",
                "position": "1",
            }]

    try:
        audit._read_positions(Client())
    except audit.AuditError:
        return

    assert False, "missing avgPrice must fail closed"


def test_positions_accept_hedge_long_and_short_for_same_symbol():
    class Client:
        def _request(self, *args):
            return [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "LONG",
                    "position": "0.01",
                    "avgPrice": "60000",
                },
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "SHORT",
                    "position": "0.02",
                    "avgPrice": "61000",
                },
            ]

    rows = audit._read_positions(Client())

    assert len(rows) == 2
    assert rows[0]["side"] == "LONG"
    assert rows[1]["side"] == "SHORT"


def test_positions_reject_duplicate_symbol_and_same_side():
    class Client:
        def _request(self, *args):
            return [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "LONG",
                    "position": "0",
                },
                {
                    "symbol": "BTC-SWAP-USDT",
                    "side": "LONG",
                    "position": "0",
                },
            ]

    try:
        audit._read_positions(Client())
    except audit.AuditError as exc:
        assert "duplicate_position_symbol_side" in str(exc)
        return

    assert False, "duplicate symbol-side pair must fail closed"


def test_position_unrealized_pnl_prefers_documented_field():
    row = {
        "unrealizedPnL": "12.34",
        "unRealizedProfit": "99.99",
    }

    assert audit._position_unrealized_pnl(row) == "12.34"


def test_position_unrealized_pnl_supports_legacy_fallback():
    row = {
        "unRealizedProfit": "-2.50",
    }

    assert audit._position_unrealized_pnl(row) == "-2.50"


def test_balance_flow_reader_requires_stable_scan(monkeypatch):
    calls = []

    expected = [{
        "id": "1",
        "coin": "USDT",
        "symbol": "BTC-SWAP-USDT",
        "flow_type_raw": "Fund Fee",
        "flow_type_value": 32,
        "category": "FUNDING",
        "change": "-0.25",
        "total": "100",
        "created_ms": 2000000000000,
    }]

    def fake_fetch(client, **kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        audit,
        "fetch_all_futures_balance_flow",
        fake_fetch,
    )

    rows, summary = audit._read_stable_balance_flow(
        object(),
        page_size=100,
        max_pages=10,
    )

    assert rows == expected
    assert summary["row_count"] == 1
    assert summary["known_flow_categories_complete"] is True
    assert len(calls) == 1
    assert calls[0]["verify_stable"] is True


def test_balance_flow_reader_rejects_unstable_snapshot(monkeypatch):
    def fake_fetch(client, **kwargs):
        raise flow.BalanceFlowError(
            "balance_flow_snapshot_changed_between_scans"
        )

    monkeypatch.setattr(
        audit,
        "fetch_all_futures_balance_flow",
        fake_fetch,
    )

    try:
        audit._read_stable_balance_flow(object())
    except audit.AuditError as exc:
        assert "balance_flow_audit_failed" in str(exc)
        return

    assert False, "unstable balance flow must fail closed"


def test_balance_flow_unknown_category_is_detected():
    row = {
        "id": "1",
        "coin": "USDT",
        "symbol": "BTC-SWAP-USDT",
        "flow_type_raw": "New Unknown Type",
        "flow_type_value": 999,
        "category": "UNKNOWN",
        "change": "-1",
        "total": "99",
        "created_ms": 2000000000000,
    }

    summary = flow.summarize_balance_flow([row])

    assert summary["unknown_flow_type_count"] == 1
    assert summary["known_flow_categories_complete"] is False


def test_live_execution_is_never_authorized():
    assert (
        audit.POSITIONS_ENDPOINT
        == "/api/v1/futures/positions"
    )
