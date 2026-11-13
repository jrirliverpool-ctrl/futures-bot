
import pytest

from src.funding_quality import (
    analyze_funding_records,
    funding_cashflow_usdt,
)

BASE = 1_700_000_000_000
HOUR = 3_600_000


def make_records(period_hours=8, count=5, start=BASE):
    return [
        {
            "id": i + 1,
            "settleTime": start + i * period_hours * HOUR,
            "settleRate": 0.0001,
            "period": f"{period_hours}H",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("period", [1, 4, 8])
def test_supported_periods(period):
    result = analyze_funding_records(make_records(period))

    assert result["period"]["hours"] == period
    assert result["period"]["source"] == "exchange_metadata"
    assert result["record_counts"]["unique_settlements"] == 5
    assert result["coverage"]["missing_expected_settlements"] == 0


def test_16_hour_gap_means_one_missing_settlement():
    records = make_records(8, 5)
    records.pop(2)

    result = analyze_funding_records(records)

    assert result["coverage"]["missing_expected_settlements"] == 1
    assert result["status"] == "usable_with_gaps"
    assert len(result["backtest_records"]) == 4


def test_24_hour_gap_means_two_missing_settlements():
    records = make_records(8, 5)
    records = records[:1] + records[4:]

    result = analyze_funding_records(records)

    assert result["coverage"]["missing_expected_settlements"] == 2


def test_32_hour_gap_means_three_missing_settlements():
    records = make_records(8, 6)
    records = records[:2] + records[5:]

    result = analyze_funding_records(records)

    assert result["coverage"]["missing_expected_settlements"] == 3


def test_12_hour_gap_is_irregular_not_three_missing():
    records = make_records(8, 4)
    records[2]["settleTime"] = (
        records[1]["settleTime"] + 12 * HOUR
    )

    result = analyze_funding_records(records)

    assert result["coverage"]["irregular_intervals"] >= 1
    assert result["coverage"]["missing_expected_settlements"] == 0
    assert result["status"] == "usable_with_irregular_intervals"


def test_single_conflicting_metadata_value_is_detected():
    records = make_records(8, 10)
    records[0]["period"] = "4H"

    result = analyze_funding_records(records)

    assert result["period"]["hours"] is None
    assert result["period"]["source"] == "mixed_exchange_metadata"
    assert result["status"] == "period_metadata_conflict"


def test_unknown_period_is_not_assumed_to_be_8h():
    records = make_records(8, 4)

    for record in records:
        record.pop("period")

    result = analyze_funding_records(records)

    assert result["period"]["hours"] is None
    assert result["period"]["source"] == "period_unknown"
    assert result["status"] == "period_unknown"


def test_configured_period_is_explicit_fallback():
    records = make_records(8, 4)

    for record in records:
        record.pop("period")

    result = analyze_funding_records(
        records,
        configured_period_hours=8,
    )

    assert result["period"]["hours"] == 8
    assert result["period"]["source"] == "configured_fallback"


def test_invalid_timestamp_does_not_delete_valid_records():
    records = make_records(8, 5)
    records.append({
        "settleTime": "invalid",
        "settleRate": 0.0001,
        "period": "8H",
    })

    result = analyze_funding_records(records)

    assert result["record_counts"]["unique_settlements"] == 5
    assert result["record_counts"]["invalid_timestamps"] == 1


def test_duplicate_same_rate_is_detected():
    records = make_records(8, 4)
    records.append(dict(records[1]))

    result = analyze_funding_records(records)

    assert result["record_counts"]["duplicate_timestamps"] == 1
    assert result["record_counts"]["conflicting_timestamps"] == 0
    assert result["record_counts"]["unique_settlements"] == 4


def test_duplicate_different_rate_is_conflict():
    records = make_records(8, 4)
    duplicate = dict(records[1])
    duplicate["settleRate"] = 0.002
    records.append(duplicate)

    result = analyze_funding_records(records)

    assert result["record_counts"]["conflicting_timestamps"] == 1
    assert result["status"] == "conflicting_duplicate_rates"


def test_explicit_expected_window_detects_boundary_missing():
    records = make_records(8, 4)

    result = analyze_funding_records(
        records,
        expected_start_ms=BASE,
        expected_end_ms=BASE + 5 * 8 * HOUR,
    )

    boundary = result["coverage"]["boundary"]

    assert boundary["boundary_coverage_known"] is True
    assert boundary["missing_expected_settlements"] == 2


def test_no_explicit_window_does_not_claim_boundary_coverage():
    result = analyze_funding_records(make_records(8, 4))

    assert result["coverage"]["boundary"]["boundary_coverage_known"] is False


def test_real_zero_rate_is_preserved():
    records = make_records(8, 3)
    records[1]["settleRate"] = 0.0

    result = analyze_funding_records(records)

    assert result["backtest_records"][1]["funding_rate"] == 0.0
    assert result["rates"]["count"] == 3


def test_long_pays_positive_funding():
    assert funding_cashflow_usdt(
        1000, "LONG", 0.001
    ) == pytest.approx(-1.0)


def test_short_receives_positive_funding():
    assert funding_cashflow_usdt(
        1000, "SHORT", 0.001
    ) == pytest.approx(1.0)


def test_negative_funding_reverses_cashflow():
    assert funding_cashflow_usdt(
        1000, "LONG", -0.001
    ) == pytest.approx(1.0)

    assert funding_cashflow_usdt(
        1000, "SHORT", -0.001
    ) == pytest.approx(-1.0)


def test_invalid_side_is_rejected():
    with pytest.raises(ValueError):
        funding_cashflow_usdt(1000, "UNKNOWN", 0.001)


def test_negative_notional_is_rejected():
    with pytest.raises(ValueError):
        funding_cashflow_usdt(-1000, "LONG", 0.001)
