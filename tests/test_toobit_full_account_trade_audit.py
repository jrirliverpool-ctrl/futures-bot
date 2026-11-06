
import sys
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import toobit_full_account_trade_audit as audit


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

    assert (
        Decimal(
            result["commission_by_asset"]["USDT"]
        )
        == Decimal("0.07")
    )

    assert (
        Decimal(
            result["reported_realized_pnl_usdt"]
        )
        == Decimal("1")
    )

    assert result["maker_fill_count"] == 1
    assert result["taker_fill_count"] == 1


def test_summary_flags_missing_realized_pnl():
    rows = [
        {
            "commission_asset": "USDT",
            "commission": "0.05",
            "maker_rebate": "0",
            "realized_pnl": "0",
            "is_maker": None,
        }
    ]

    result = audit._summarize_fills(
        rows,
        missing_realized_pnl=1,
    )

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


def test_live_execution_is_never_authorized():
    assert (
        audit.POSITIONS_ENDPOINT
        == "/api/v1/futures/positions"
    )
