
import pytest

from src.funding_quality import (
    analyze_funding_records,
    funding_cashflow_usdt,
)


BASE = 1_700_000_000_000


def make_records(period_hours, count=5):
    step = period_hours * 3_600_000

    return [
        {
            "id": i + 1,
            "settleTime": BASE + i * step,
            "settleRate": 0.0001,
            "period": f"{period_hours}H",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("period", [1, 4, 8])
def test_supported_funding_periods(period):
    result = analyze_funding_records(
        make_records(period)
    )

    assert result["period"]["hours"] == period
    assert result["record_counts"]["unique_settlements"] == 5
    assert result["coverage"]["missing_expected_settlements"] == 0
    assert result["coverage"]["coverage_is_complete"] is True


def test_16_hour_gap_in_8h_schedule():
    records = make_records(8, 5)
    records.pop(2)

    result = analyze_funding_records(records)

    assert result["period"]["hours"] == 8
    assert result["coverage"]["missing_expected_settlements"] == 1
    assert result["coverage"]["percentage_scored"] < 100
    assert result["status"] == "usable_with_gaps"


def test_32_hour_gap_means_three_missing_settlements():
    records = make_records(8, 6)
    records = records[:2] + records[5:]

    result = analyze_funding_records(records)

    assert result["coverage"]["missing_expected_settlements"] == 3


def test_irregular_12_hour_gap_is_not_assumed_to_be_period_change():
    records = make_records(8, 4)

    records[2]["settleTime"] = (
        records[1]["settleTime"] + 12 * 3_600_000
    )

    result = analyze_funding_records(records)

    assert result["period"]["hours"] == 8
    assert result["coverage"]["irregular_intervals"] >= 1
    assert result["coverage"]["coverage_is_complete"] is False


def test_mixed_period_metadata_is_detected():
    records = make_records(8, 4)
    records[2]["period"] = "4H"

    result = analyze_funding_records(records)

    assert result["period"]["source"] == "mixed_exchange_metadata"
    assert result["status"] == "period_metadata_conflict"


def test_valid_records_are_not_deleted_due_to_bad_record():
    records = make_records(8, 5)

    records.append({
        "settleTime": "invalid",
        "settleRate": 0.0001,
        "period": "8H",
    })

    result = analyze_funding_records(records)

    assert result["record_counts"]["unique_settlements"] == 5
    assert result["record_counts"]["invalid_timestamps"] == 1
    assert len(result["backtest_records"]) == 5


def test_duplicate_settlement_is_detected():
    records = make_records(8, 4)
    records.append(dict(records[1]))

    result = analyze_funding_records(records)

    assert result["record_counts"]["unique_settlements"] == 4
    assert result["record_counts"]["duplicate_timestamps"] == 1


def test_long_pays_positive_funding():
    result = funding_cashflow_usdt(
        1000,
        "LONG",
        0.001,
    )

    assert result == pytest.approx(-1.0)


def test_short_receives_positive_funding():
    result = funding_cashflow_usdt(
        1000,
        "SHORT",
        0.001,
    )

    assert result == pytest.approx(1.0)


def test_negative_funding_reverses_cashflow():
    assert funding_cashflow_usdt(
        1000, "LONG", -0.001
    ) == pytest.approx(1.0)

    assert funding_cashflow_usdt(
        1000, "SHORT", -0.001
    ) == pytest.approx(-1.0)


def test_invalid_position_side_is_rejected():
    with pytest.raises(ValueError):
        funding_cashflow_usdt(1000, "UNKNOWN", 0.001)
