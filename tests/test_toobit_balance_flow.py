
import sys
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import toobit_balance_flow as flow


def row(flow_id, flow_type=32, change="-0.5"):
    return {
        "id": str(flow_id),
        "coin": "USDT",
        "symbol": "BTC-SWAP-USDT",
        "flowType": flow_type,
        "change": change,
        "total": "100.25",
        "created": "2000000000000",
    }


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def _request(self, method, path, params):
        self.calls.append((method, path, dict(params)))
        if not self.pages:
            raise AssertionError("unexpected extra request")
        return self.pages.pop(0)


def test_normalize_known_funding_flow():
    result = flow.normalize_flow_row(row(10))

    assert result["id"] == "10"
    assert result["category"] == "FUNDING"
    assert result["change"] == "-0.5"
    assert result["coin"] == "USDT"


def test_normalize_known_fee_and_realized_pnl_codes():
    assert flow.normalize_flow_row(
        row(11, 10, "-0.2")
    )["category"] == "FEE"

    assert flow.normalize_flow_row(
        row(12, 28, "2.5")
    )["category"] == "REALIZED_PNL"


def test_unknown_flow_type_is_preserved_not_guessed():
    result = flow.normalize_flow_row(
        row(13, "UNRECOGNIZED_TYPE", "1")
    )

    assert result["category"] == "UNKNOWN"
    assert result["flow_type_raw"] == "UNRECOGNIZED_TYPE"


def test_invalid_flow_row_fails_closed():
    bad = row(10)
    bad["change"] = "NaN"

    try:
        flow.normalize_flow_row(bad)
    except flow.BalanceFlowError:
        return

    assert False, "non-finite change must be rejected"


def test_negative_total_rejected():
    bad = row(10)
    bad["total"] = "-1"

    try:
        flow.normalize_flow_row(bad)
    except flow.BalanceFlowError:
        return

    assert False, "negative total must be rejected"


def test_descending_pagination_uses_from_id():
    client = FakeClient([
        [row(30), row(20)],
        [row(10)],
    ])

    result = flow.fetch_all_futures_balance_flow(
        client,
        page_size=2,
        verify_stable=False,
    )

    assert [item["id"] for item in result] == [
        "30", "20", "10"
    ]

    assert client.calls[1][2]["fromId"] == 20


def test_stable_scan_requires_two_matching_snapshots():
    client = FakeClient([
        [row(30), row(20)],
        [row(10)],
        [row(30), row(20)],
        [row(10)],
    ])

    result = flow.fetch_all_futures_balance_flow(
        client,
        page_size=2,
        verify_stable=True,
    )

    assert len(result) == 3
    assert len(client.calls) == 4


def test_changed_snapshot_is_rejected():
    client = FakeClient([
        [row(30), row(20)],
        [row(10)],
        [row(31), row(20)],
        [row(10)],
    ])

    try:
        flow.fetch_all_futures_balance_flow(
            client,
            page_size=2,
            verify_stable=True,
        )
    except flow.BalanceFlowError as exc:
        assert "changed_between_scans" in str(exc)
        return

    assert False, "changed snapshots must fail closed"


def test_duplicate_ids_are_rejected():
    client = FakeClient([
        [row(30), row(30)],
    ])

    try:
        flow.fetch_all_futures_balance_flow(
            client,
            page_size=2,
            verify_stable=False,
        )
    except flow.BalanceFlowError:
        return

    assert False, "duplicate flow IDs must be rejected"


def test_non_descending_page_is_rejected():
    client = FakeClient([
        [row(20), row(30)],
    ])

    try:
        flow.fetch_all_futures_balance_flow(
            client,
            page_size=2,
            verify_stable=False,
        )
    except flow.BalanceFlowError:
        return

    assert False, "ascending IDs must be rejected"


def test_transport_unknown_is_rejected():
    client = FakeClient([
        {
            "_transport": "UNKNOWN",
            "error": "timeout",
        },
    ])

    try:
        flow.fetch_all_futures_balance_flow(
            client,
            verify_stable=False,
        )
    except flow.BalanceFlowError:
        return

    assert False, "unknown transport must fail closed"


def test_summary_aggregates_funding_fee_and_pnl():
    rows = [
        flow.normalize_flow_row(row(1, 32, "-0.5")),
        flow.normalize_flow_row(row(2, 10, "-0.2")),
        flow.normalize_flow_row(row(3, 28, "2.5")),
        flow.normalize_flow_row(row(4, 700, "-3")),
        flow.normalize_flow_row(row(5, 701, "-1")),
    ]

    result = flow.summarize_balance_flow(rows)
    totals = result["category_change_by_coin"]["USDT"]

    assert Decimal(totals["FUNDING"]) == Decimal("-0.5")
    assert Decimal(totals["FEE"]) == Decimal("-0.2")
    assert Decimal(totals["REALIZED_PNL"]) == Decimal("2.5")
    assert Decimal(totals["LIQUIDATION"]) == Decimal("-3")
    assert Decimal(totals["ADL"]) == Decimal("-1")
    assert result["known_flow_categories_complete"] is True


def test_summary_marks_unknown_types_incomplete():
    rows = [
        flow.normalize_flow_row(
            row(1, "UNRECOGNIZED_TYPE", "1")
        ),
    ]

    result = flow.summarize_balance_flow(rows)

    assert result["unknown_flow_type_count"] == 1
    assert result["known_flow_categories_complete"] is False


def test_invalid_page_size_rejected():
    client = FakeClient([])

    try:
        flow.fetch_all_futures_balance_flow(
            client,
            page_size=1001,
        )
    except ValueError:
        return

    assert False, "page size above API limit must be rejected"
