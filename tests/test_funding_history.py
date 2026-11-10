import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from funding_history import FundingHistoryError, load_funding_report


SYMBOL = "BTC-SWAP-USDT"
PERIOD = 28_800_000
BASE = PERIOD * 1_000


def _row(row_id, settle_time, settle_rate="0.0001", **overrides):
    data = {
        "id": str(row_id),
        "symbol": SYMBOL,
        "settleTime": str(settle_time),
        "settleRate": settle_rate,
        "period": "8H",
    }
    data.update(overrides)
    return data


def _write_report(tmp_path, rows, **overrides):
    report = {
        "symbol": SYMBOL,
        "endpoint_ok": True,
        "pagination_verified": True,
        "data_quality_ok": True,
        "fetched_at_iso": "2026-11-10T00:00:00+00:00",
        "page_records": [{
            "error": None,
            "response_json_raw": rows,
        }],
    }
    report.update(overrides)
    path = tmp_path / "funding-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_load_complete_funding_history(tmp_path):
    path = _write_report(tmp_path, [
        _row(1, BASE),
        _row(2, BASE + PERIOD, "-0.0002"),
        _row(3, BASE + PERIOD * 2, "0"),
    ])
    result = load_funding_report(
        path,
        symbol=SYMBOL,
        start_ms=BASE,
        end_ms=BASE + PERIOD * 2,
        expected_period_ms=PERIOD,
    )
    assert result["funding_rates"] == {
        BASE: 0.0001,
        BASE + PERIOD: -0.0002,
        BASE + PERIOD * 2: 0.0,
    }
    assert result["row_count"] == 3


@pytest.mark.parametrize(
    "report_overrides, rows, message",
    [
        ({"pagination_verified": False}, [_row(1, BASE), _row(2, BASE + PERIOD)], "pagination"),
        ({"data_quality_ok": False}, [_row(1, BASE), _row(2, BASE + PERIOD)], "data_quality"),
        ({}, [_row(1, BASE), _row(2, BASE + PERIOD * 2)], "gaps"),
        ({}, [_row(1, BASE), _row(1, BASE + PERIOD)], "duplicate_id"),
        ({}, [_row(1, BASE), _row(2, BASE + PERIOD)], "does_not_cover_interval"),
        ({}, [_row(1, BASE), _row(2, BASE + PERIOD, period="4H")], "period_inconsistent"),
        ({}, [_row(1, BASE, "NaN"), _row(2, BASE + PERIOD)], "not_finite"),
    ],
)
def test_invalid_or_incomplete_history_fails_closed(
    tmp_path, report_overrides, rows, message
):
    path = _write_report(tmp_path, rows, **report_overrides)
    start_ms = BASE
    end_ms = (
        BASE + PERIOD * 2
        if "does_not_cover_interval" in message
        else int(rows[-1]["settleTime"])
    )
    with pytest.raises(FundingHistoryError, match=message):
        load_funding_report(
            path,
            symbol=SYMBOL,
            start_ms=start_ms,
            end_ms=end_ms,
        )


def test_wrong_symbol_and_expected_period_are_rejected(tmp_path):
    path = _write_report(tmp_path, [_row(1, BASE), _row(2, BASE + PERIOD)])
    with pytest.raises(FundingHistoryError, match="symbol_mismatch"):
        load_funding_report(
            path, symbol="ETH-SWAP-USDT", start_ms=BASE, end_ms=BASE + PERIOD
        )
    with pytest.raises(FundingHistoryError, match="does_not_match_expected"):
        load_funding_report(
            path,
            symbol=SYMBOL,
            start_ms=BASE,
            end_ms=BASE + PERIOD,
            expected_period_ms=PERIOD // 2,
        )
