"""Strict loader for archived Toobit funding-history probe reports."""

import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


_PERIOD_RE = re.compile(r"^(\d+)([MH])$", re.IGNORECASE)


class FundingHistoryError(ValueError):
    """Funding history is incomplete or inconsistent with the request."""


def _items_from_page(page):
    raw = page.get("response_json_raw")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("data", "result", "items", "list"):
            if isinstance(raw.get(key), list):
                return raw[key]
    raise FundingHistoryError("funding_probe_page_items_invalid")


def _period_ms(value):
    if not isinstance(value, str):
        raise FundingHistoryError("funding_period_missing")
    match = _PERIOD_RE.fullmatch(value.strip())
    if not match:
        raise FundingHistoryError("funding_period_invalid")
    count = int(match.group(1))
    if count <= 0:
        raise FundingHistoryError("funding_period_invalid")
    unit_ms = 60_000 if match.group(2).upper() == "M" else 3_600_000
    return count * unit_ms


def _rate(value):
    if value is None or isinstance(value, bool):
        raise FundingHistoryError("funding_rate_missing_or_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise FundingHistoryError("funding_rate_not_numeric") from None
    if not result.is_finite():
        raise FundingHistoryError("funding_rate_not_finite")
    return result


def load_funding_report(
    path,
    *,
    symbol,
    start_ms,
    end_ms,
    expected_period_ms=None,
):
    """Load a probe report and return rates for a fully covered interval.

    This intentionally refuses reports whose pagination/data-quality probes
    did not pass, have gaps, contain duplicate IDs/times, or do not cover the
    requested candle interval. Returned rates use the timestamp keys expected
    by ``backtest.run_backtest``.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms < start_ms
    ):
        raise ValueError("invalid funding coverage interval")
    if expected_period_ms is not None and (
        isinstance(expected_period_ms, bool)
        or not isinstance(expected_period_ms, int)
        or expected_period_ms <= 0
    ):
        raise ValueError("expected_period_ms must be a positive integer")

    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FundingHistoryError(
            f"funding_probe_report_unreadable:{type(exc).__name__}"
        ) from None

    if not isinstance(report, dict) or report.get("symbol") != symbol:
        raise FundingHistoryError("funding_probe_symbol_mismatch")
    for field in ("endpoint_ok", "pagination_verified", "data_quality_ok"):
        if report.get(field) is not True:
            raise FundingHistoryError(f"funding_probe_{field}_not_verified")

    pages = report.get("page_records")
    if not isinstance(pages, list) or not pages:
        raise FundingHistoryError("funding_probe_pages_missing")

    rows = []
    for page in pages:
        if not isinstance(page, dict) or page.get("error"):
            raise FundingHistoryError("funding_probe_page_failed")
        rows.extend(_items_from_page(page))

    ids = set()
    by_time = {}
    periods = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") != symbol:
            raise FundingHistoryError("funding_row_symbol_or_shape_invalid")
        raw_id = row.get("id")
        if raw_id is None or not str(raw_id).isdigit():
            raise FundingHistoryError("funding_row_id_invalid")
        row_id = str(raw_id)
        if row_id in ids:
            raise FundingHistoryError("funding_duplicate_id")
        ids.add(row_id)

        raw_ts = row.get("settleTime")
        if isinstance(raw_ts, bool) or raw_ts is None:
            raise FundingHistoryError("funding_settle_time_invalid")
        try:
            timestamp = int(raw_ts)
        except (TypeError, ValueError, OverflowError):
            raise FundingHistoryError("funding_settle_time_invalid") from None
        if timestamp < 10_000_000_000:
            timestamp *= 1000
        if timestamp <= 0 or timestamp in by_time:
            raise FundingHistoryError("funding_duplicate_or_invalid_settle_time")

        periods.add(_period_ms(row.get("period")))
        rate = float(_rate(row.get("settleRate")))
        if not math.isfinite(rate):
            raise FundingHistoryError("funding_rate_not_finite")
        by_time[timestamp] = rate

    if len(periods) != 1:
        raise FundingHistoryError("funding_period_inconsistent")
    period_ms = periods.pop()
    if expected_period_ms is not None and period_ms != expected_period_ms:
        raise FundingHistoryError("funding_period_does_not_match_expected")

    timestamps = sorted(by_time)
    if len(timestamps) < 2:
        raise FundingHistoryError("funding_history_too_short_to_verify")
    if any(b - a != period_ms for a, b in zip(timestamps, timestamps[1:])):
        raise FundingHistoryError("funding_history_has_gaps_or_irregular_periods")

    if timestamps[0] > start_ms or timestamps[-1] < end_ms:
        raise FundingHistoryError("funding_history_does_not_cover_interval")

    selected = {
        timestamp: by_time[timestamp]
        for timestamp in timestamps
        if start_ms <= timestamp <= end_ms
    }
    # Use the exchange's observed settlement phase instead of assuming that
    # all contracts settle on Unix-epoch multiples of the period.
    phase = timestamps[0] % period_ms
    first_expected = start_ms + ((phase - start_ms) % period_ms)
    expected_times = range(first_expected, end_ms + 1, period_ms)
    missing = [timestamp for timestamp in expected_times if timestamp not in selected]
    if missing:
        raise FundingHistoryError(
            "funding_history_missing_settlements: "
            + ",".join(str(timestamp) for timestamp in missing[:5])
        )

    return {
        "symbol": symbol,
        "funding_period_ms": period_ms,
        "funding_rates": selected,
        "coverage_start_ms": timestamps[0],
        "coverage_end_ms": timestamps[-1],
        "row_count": len(timestamps),
        "probe_fetched_at": report.get("fetched_at_iso"),
    }
