
"""Funding history quality analysis.

Read-only data analysis. Never fabricates settlement records or fills
missing funding rates with zero.

Coverage is measured only inside an explicitly supplied expected window.
Without such a window, only internal gaps between observed settlements
are measured; boundary completeness remains unknown.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
import math
import re

SCHEMA_VERSION = "2.0"
TIMESTAMP_TOLERANCE_MS = 60_000


def parse_period_hours(value: Any) -> float | None:
    if value is None:
        return None

    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*H\s*",
        str(value),
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    hours = float(match.group(1))
    if not math.isfinite(hours) or hours <= 0:
        return None

    return hours


def normalize_timestamp_ms(value: Any) -> int | None:
    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if not math.isfinite(number) or number <= 0:
        return None

    if number < 10_000_000_000:
        number *= 1000

    if number > 100_000_000_000_000:
        return None

    return int(number)


def timestamp_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def _period_distribution(records: list[Any]) -> tuple[Counter, int]:
    distribution = Counter()
    unrecognized = 0

    for record in records:
        if not isinstance(record, dict):
            continue

        value = record.get("period")

        # Missing metadata is different from conflicting metadata.
        if value is None or str(value).strip() == "":
            continue

        hours = parse_period_hours(value)

        if hours is None:
            unrecognized += 1
        else:
            distribution[str(hours)] += 1

    return distribution, unrecognized


def _select_period(
    distribution: Counter,
    unrecognized_count: int,
    configured_period_hours: float | None,
) -> tuple[float | None, str]:
    if len(distribution) > 1:
        return None, "mixed_exchange_metadata"

    if unrecognized_count:
        return None, "unrecognized_exchange_metadata"

    if len(distribution) == 1:
        value = next(iter(distribution))
        return float(value), "exchange_metadata"

    if configured_period_hours is not None:
        try:
            configured = float(configured_period_hours)
        except (TypeError, ValueError, OverflowError):
            return None, "invalid_configuration"

        if math.isfinite(configured) and configured > 0:
            return configured, "configured_fallback"

        return None, "invalid_configuration"

    return None, "period_unknown"


def analyze_funding_records(
    records: Iterable[dict[str, Any]],
    *,
    configured_period_hours: float | None = None,
    expected_start_ms: int | None = None,
    expected_end_ms: int | None = None,
) -> dict[str, Any]:
    """Analyze one symbol's raw funding settlements."""

    raw = list(records)

    if (
        expected_start_ms is not None
        and expected_end_ms is not None
        and expected_end_ms < expected_start_ms
    ):
        raise ValueError("expected_end_ms must be >= expected_start_ms")

    valid = []
    invalid_timestamps = 0
    invalid_rates = 0
    invalid_records = 0

    for source in raw:
        if not isinstance(source, dict):
            invalid_records += 1
            continue

        timestamp = normalize_timestamp_ms(source.get("settleTime"))

        if timestamp is None:
            invalid_timestamps += 1
            continue

        try:
            rate = float(source.get("settleRate"))
            if not math.isfinite(rate):
                raise ValueError("non-finite rate")
        except (TypeError, ValueError, OverflowError):
            invalid_rates += 1
            continue

        item = dict(source)
        item["settle_time_ms"] = timestamp
        item["settle_time_utc"] = timestamp_iso(timestamp)
        item["funding_rate"] = rate
        valid.append(item)

    # Group by settlement timestamp. Conflicting rates are never silently
    # resolved by choosing whichever record arrived first.
    grouped = defaultdict(list)

    for item in valid:
        grouped[item["settle_time_ms"]].append(item)

    normalized = []
    duplicate_timestamps = []
    conflicting_timestamps = []

    for timestamp in sorted(grouped):
        group = grouped[timestamp]
        rates = {item["funding_rate"] for item in group}

        if len(group) > 1:
            duplicate_timestamps.append(timestamp)

        if len(rates) > 1:
            conflicting_timestamps.append(timestamp)
            continue

        normalized.append(group[0])

    distribution, unrecognized_metadata = _period_distribution(raw)

    period_hours, period_source = _select_period(
        distribution,
        unrecognized_metadata,
        configured_period_hours,
    )

    missing_expected = 0
    irregular_gaps = []
    gap_details = []
    scored_intervals = 0

    if period_hours is not None:
        cadence_ms = period_hours * 3_600_000

        for previous, current in zip(normalized, normalized[1:]):
            delta_ms = (
                current["settle_time_ms"]
                - previous["settle_time_ms"]
            )

            nearest = round(delta_ms / cadence_ms)

            aligned = (
                nearest >= 1
                and abs(delta_ms - nearest * cadence_ms)
                <= TIMESTAMP_TOLERANCE_MS
            )

            if not aligned:
                irregular_gaps.append({
                    "from_utc": previous["settle_time_utc"],
                    "to_utc": current["settle_time_utc"],
                    "elapsed_hours": round(
                        delta_ms / 3_600_000, 6
                    ),
                    "classification": "irregular_or_schedule_shift",
                })
                continue

            scored_intervals += 1

            missing = max(0, nearest - 1)
            missing_expected += missing

            if missing:
                gap_details.append({
                    "from_utc": previous["settle_time_utc"],
                    "to_utc": current["settle_time_utc"],
                    "elapsed_hours": round(
                        delta_ms / 3_600_000, 6
                    ),
                    "expected_missing": missing,
                    "classification": "missing_settlements",
                })

    # Internal coverage: only when the schedule is known and all
    # adjacent observed intervals can be scored.
    internal_expected = (
        len(normalized) + missing_expected
        if normalized and period_hours is not None
        else None
    )

    internal_coverage = (
        len(normalized) / internal_expected * 100
        if internal_expected
        else None
    )

    # Optional explicit expected window.
    boundary = {
        "expected_start_ms": expected_start_ms,
        "expected_end_ms": expected_end_ms,
        "expected_settlements": None,
        "observed_settlements_in_window": None,
        "missing_expected_settlements": None,
        "coverage_percentage": None,
        "boundary_coverage_known": False,
    }

    if (
        period_hours is not None
        and expected_start_ms is not None
        and expected_end_ms is not None
    ):
        cadence_ms = period_hours * 3_600_000

        expected_count = (
            max(0, int(
                (expected_end_ms - expected_start_ms)
                // cadence_ms
            )) + 1
        )

        observed_in_window = [
            item for item in normalized
            if expected_start_ms <= item["settle_time_ms"]
            <= expected_end_ms
        ]

        observed_times = {
            item["settle_time_ms"] for item in observed_in_window
        }

        expected_times = {
            expected_start_ms + i * int(cadence_ms)
            for i in range(expected_count)
            if expected_start_ms + i * cadence_ms
            <= expected_end_ms + TIMESTAMP_TOLERANCE_MS
        }

        missing_boundary = len(expected_times - observed_times)

        boundary = {
            "expected_start_ms": expected_start_ms,
            "expected_end_ms": expected_end_ms,
            "expected_settlements": len(expected_times),
            "observed_settlements_in_window": len(observed_times),
            "missing_expected_settlements": missing_boundary,
            "coverage_percentage": (
                round(
                    len(observed_times) / len(expected_times) * 100,
                    6,
                )
                if expected_times else None
            ),
            "boundary_coverage_known": True,
        }

    rates = [item["funding_rate"] for item in normalized]

    metadata_conflict = period_source in {
        "mixed_exchange_metadata",
        "unrecognized_exchange_metadata",
        "invalid_configuration",
    }

    if not normalized:
        status = "no_valid_data"
    elif metadata_conflict:
        status = "period_metadata_conflict"
    elif conflicting_timestamps:
        status = "conflicting_duplicate_rates"
    elif period_hours is None:
        status = "period_unknown"
    elif irregular_gaps:
        status = "usable_with_irregular_intervals"
    elif missing_expected or (
        boundary["missing_expected_settlements"] or 0
    ):
        status = "usable_with_gaps"
    else:
        status = "complete_on_observed_schedule"

    return {
        "schema_version": SCHEMA_VERSION,
        "record_counts": {
            "raw": len(raw),
            "valid": len(valid),
            "unique_settlements": len(normalized),
            "duplicate_timestamps": len(duplicate_timestamps),
            "conflicting_timestamps": len(conflicting_timestamps),
            "invalid_timestamps": invalid_timestamps,
            "invalid_rates": invalid_rates,
            "invalid_records": invalid_records,
        },
        "period": {
            "hours": period_hours,
            "source": period_source,
            "metadata_distribution": dict(distribution),
            "unrecognized_metadata_count": unrecognized_metadata,
        },
        "coverage": {
            "internal_percentage": (
                round(internal_coverage, 6)
                if internal_coverage is not None else None
            ),
            "internal_expected_settlements": internal_expected,
            "missing_expected_settlements": missing_expected,
            "scored_intervals": scored_intervals,
            "irregular_intervals": len(irregular_gaps),
            "boundary": boundary,
        },
        "gaps": {
            "missing_settlements": gap_details,
            "irregular_intervals": irregular_gaps,
            "conflicting_duplicate_timestamps_ms": conflicting_timestamps,
        },
        "rates": {
            "count": len(rates),
            "min": min(rates) if rates else None,
            "max": max(rates) if rates else None,
            "mean": sum(rates) / len(rates) if rates else None,
        },
        "status": status,
        "backtest_records": normalized,
    }


def funding_cashflow_usdt(
    notional_usdt: float,
    side: str,
    funding_rate: float,
) -> float:
    """Positive = received; negative = paid."""

    notional = float(notional_usdt)
    rate = float(funding_rate)
    direction = str(side).upper()

    if not math.isfinite(notional) or notional < 0:
        raise ValueError("notional_usdt must be finite and >= 0")

    if not math.isfinite(rate):
        raise ValueError("funding_rate must be finite")

    if direction == "LONG":
        sign = -1.0
    elif direction == "SHORT":
        sign = 1.0
    else:
        raise ValueError("side must be LONG or SHORT")

    return sign * notional * rate
