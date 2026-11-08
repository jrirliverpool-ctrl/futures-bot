
import sys
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import toobit_balance_flow as flow


def row(
    flow_id,
    flow_type_value=32,
    flow_type="Fund Fee",
    change="-0.5",
):
    return {
        "id": str(flow_id),
        "coin": "USDT",
        "symbol": "BTC-SWAP-USDT",
        "flowTypeValue": flow_type_value,
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


def test_normalize_numeric_flow_type_value():
    result = flow.normalize_flow_row(row(10))

    assert result["id"] == "10"
    assert result["category"] == "FUNDING"
    assert result["flow_type_value"] == 32
    assert result["change"] == "-0.5"


def test_normalize_text_only_known_flow_type():
    raw = row(
        11,
        flow_type_value=None,
        flow_type="Realized PNL",
        change="2",
    )

    result = flow.normalize_flow_row(raw)

    assert result["category"] == "REALIZED_PNL"


def test_numeric_and_text_flow_type_mismatch_rejected():
    raw = row(
        12,
        flow_type_value=10,
        flow_type="Fund Fee",
    )

    try:
        flow.normalize_flow_row(raw)
    except flow.BalanceFlowError as exc:
        assert "mismatch" in str(exc)
        return

    assert False, "mismatched flow type fields must fail"


def test_unknown_flow_type_is_preserved():
    raw = row(
        13,
        flow_type_value=999,
        flow_type="Some New Flow",
    )

    result = flow.normalize_flow_row(raw)

    assert result["category"] == "UNKNOWN"
    assert result["flow_type_value"] == 999


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


def test_symbol_filter_uses_documented_parameter():
    client = FakeClient([[]])

    flow.fetch_all_futures_balance_flow(
        client,
        symbol="BTC-SWAP-USDT",
        verify_stable=False,
    )

    assert client.calls[0][2]["symbol"] == "BTC-SWAP-USDT"
    assert "coin" not in client.calls[0][2]


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


def test_summary_aggregates_known_flow_categories():
    rows = [
        flow.normalize_flow_row(
            row(1, 32, "Fund Fee", "-0.5")
        ),
        flow.normalize_flow_row(
            row(2, 10, "Fee", "-0.2")
        ),
        flow.normalize_flow_row(
            row(3, 28, "Realized PNL", "2.5")
        ),
        flow.normalize_flow_row(
            row(4, 700, "Force liquidation", "-3")
        ),
        flow.normalize_flow_row(
            row(5, 701, "ADL", "-1")
        ),
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
            row(1, 999, "Unknown Flow", "1")
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
