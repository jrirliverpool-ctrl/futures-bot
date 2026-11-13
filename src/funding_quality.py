
"""Funding history quality analysis.

Read-only analysis. Never fabricates settlements or replaces missing rates with zero.
Internal coverage describes only the observed interval; it does not prove full
contract-history coverage unless an explicit expected window is supplied.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
import math
import re

SCHEMA_VERSION = "3.0"
TIMESTAMP_TOLERANCE_MS = 60_000
DEFAULT_MIN_INTERNAL_COVERAGE = 99.5


def parse_period_hours(value: Any) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*H\s*", str(value), re.I)
    if not match:
        return None
    hours = float(match.group(1))
    return hours if math.isfinite(hours) and hours > 0 else None


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
        timestamp_ms / 1000, tz=timezone.utc
    ).isoformat()


def _period_distribution(records: list[Any]):
    distribution = Counter()
    unrecognized = 0
    missing = 0

    for record in records:
        if not isinstance(record, dict):
            continue
        value = record.get("period")
        if value is None or str(value).strip() == "":
            missing += 1
            continue
        hours = parse_period_hours(value)
        if hours is None:
            unrecognized += 1
        else:
            distribution[str(hours)] += 1

    return distribution, unrecognized, missing


def _select_period(
    distribution,
    unrecognized_count,
    missing_count,
    configured_period_hours,
):
    if len(distribution) > 1:
        return None, "mixed_exchange_metadata"
    if unrecognized_count:
        return None, "unrecognized_exchange_metadata"
    if missing_count and distribution:
        return None, "incomplete_exchange_metadata"
    if len(distribution) == 1:
        return float(next(iter(distribution))), "exchange_metadata"

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

    raw = list(records)

    if (expected_start_ms is None) != (expected_end_ms is None):
        raise ValueError(
            "expected_start_ms and expected_end_ms must be supplied together"
        )

    if (
        expected_start_ms is not None
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

    grouped = defaultdict(list)

    for item in valid:
        grouped[item["settle_time_ms"]].append(item)

    normalized = []
    duplicate_timestamps = []
    conflicting_timestamps = []

    for timestamp in sorted(grouped):
        group = grouped[timestamp]

        if len(group) > 1:
            duplicate_timestamps.append(timestamp)

        rates = {item["funding_rate"] for item in group}

        if len(rates) > 1:
            conflicting_timestamps.append(timestamp)
            continue

        normalized.append(group[0])

    distribution, unrecognized_metadata, missing_metadata = (
        _period_distribution(raw)
    )

    period_hours, period_source = _select_period(
        distribution,
        unrecognized_metadata,
        missing_metadata,
        configured_period_hours,
    )

    missing_expected = 0
    irregular_gaps = []
    gap_details = []
    scored_intervals = 0

    if period_hours is not None:
        cadence_ms = period_hours * 3_600_000

        for previous, current in zip(
            normalized, normalized[1:]
        ):
            delta_ms = (
                current["settle_time_ms"]
                - previous["settle_time_ms"]
            )

            nearest = round(delta_ms / cadence_ms)

            aligned = (
                nearest >= 1
                and abs(
                    delta_ms - nearest * cadence_ms
                ) <= TIMESTAMP_TOLERANCE_MS
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

    # A percentage is withheld if any interval could not be scored.
    coverage_scoring_complete = bool(
        len(normalized) >= 2
        and period_hours is not None
        and not irregular_gaps
    )

    internal_expected = (
        len(normalized) + missing_expected
        if coverage_scoring_complete
        else None
    )

    internal_coverage = (
        len(normalized) / internal_expected * 100
        if internal_expected else None
    )

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
    ):
        cadence_ms = period_hours * 3_600_000

        expected_count = max(
            0,
            int(
                (expected_end_ms - expected_start_ms)
                // cadence_ms
            ),
        ) + 1

        expected_times = {
            expected_start_ms + i * int(cadence_ms)
            for i in range(expected_count)
            if expected_start_ms + i * cadence_ms
            <= expected_end_ms + TIMESTAMP_TOLERANCE_MS
        }

        observed_times = {
            item["settle_time_ms"]
            for item in normalized
            if expected_start_ms
            <= item["settle_time_ms"]
            <= expected_end_ms
        }

        missing_boundary = len(
            expected_times - observed_times
        )

        boundary = {
            "expected_start_ms": expected_start_ms,
            "expected_end_ms": expected_end_ms,
            "expected_settlements": len(expected_times),
            "observed_settlements_in_window": len(observed_times),
            "missing_expected_settlements": missing_boundary,
            "coverage_percentage": (
                round(
                    len(observed_times)
                    / len(expected_times) * 100,
                    6,
                )
                if expected_times else None
            ),
            "boundary_coverage_known": True,
        }

    rates = [
        item["funding_rate"] for item in normalized
    ]

    metadata_conflict = period_source in {
        "mixed_exchange_metadata",
        "unrecognized_exchange_metadata",
        "incomplete_exchange_metadata",
        "invalid_configuration",
    }

    structural_data_usable = bool(
        normalized
        and period_hours is not None
        and not metadata_conflict
        and invalid_timestamps == 0
        and invalid_rates == 0
        and invalid_records == 0
        and not conflicting_timestamps
    )

    if not normalized:
        status = "no_valid_data"
    elif metadata_conflict:
        status = "period_metadata_conflict"
    elif conflicting_timestamps:
        status = "conflicting_duplicate_rates"
    elif period_hours is None:
        status = "period_unknown"
    elif irregular_gaps:
        status = "quarantined_irregular_intervals"
    elif missing_expected:
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
            "missing_metadata_count": missing_metadata,
        },
        "coverage": {
            "internal_percentage": (
                round(internal_coverage, 6)
                if internal_coverage is not None
                else None
            ),
            "internal_expected_settlements": internal_expected,
            "missing_expected_settlements": missing_expected,
            "scored_intervals": scored_intervals,
            "irregular_intervals": len(irregular_gaps),
            "scoring_complete": coverage_scoring_complete,
            "boundary": boundary,
        },
        "gaps": {
            "missing_settlements": gap_details,
            "irregular_intervals": irregular_gaps,
            "conflicting_duplicate_timestamps_ms": (
                conflicting_timestamps
            ),
        },
        "rates": {
            "count": len(rates),
            "min": min(rates) if rates else None,
            "max": max(rates) if rates else None,
            "mean": sum(rates) / len(rates) if rates else None,
        },
        "data_quality_usable": structural_data_usable,
        "status": status,
        "backtest_records": normalized,
    }


def assess_backtest_eligibility(
    quality: dict[str, Any],
    *,
    endpoint_ok: bool,
    collection_complete: bool,
    min_internal_coverage: float = DEFAULT_MIN_INTERNAL_COVERAGE,
) -> dict[str, Any]:

    try:
        threshold = float(min_internal_coverage)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "min_internal_coverage must be between 0 and 100"
        )

    if not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError(
            "min_internal_coverage must be between 0 and 100"
        )

    coverage = quality.get("coverage") or {}
    counts = quality.get("record_counts") or {}
    period = quality.get("period") or {}

    internal = coverage.get("internal_percentage")
    reasons = []

    if not endpoint_ok:
        reasons.append("funding_endpoint_not_ok")

    if not quality.get("data_quality_usable", False):
        reasons.append("structural_data_quality_failed")

    if not collection_complete:
        reasons.append("funding_collection_incomplete")

    if period.get("hours") is None:
        reasons.append("funding_period_unknown")

    if counts.get("unique_settlements", 0) < 2:
        reasons.append("insufficient_settlements_for_coverage")

    if coverage.get("irregular_intervals", 0) > 0:
        reasons.append("irregular_intervals_quarantined")

    if not coverage.get("scoring_complete", False):
        reasons.append("internal_coverage_not_fully_scored")

    if internal is None or internal < threshold:
        reasons.append("internal_coverage_below_threshold")

    boundary_known = bool(
        (coverage.get("boundary") or {}).get(
            "boundary_coverage_known"
        )
    )

    return {
        "backtest_eligible": not reasons,
        "minimum_internal_coverage_percentage": threshold,
        "observed_internal_coverage_percentage": internal,
        "historical_boundary_known": boundary_known,
        "eligibility_scope": (
            "explicit_historical_window"
            if boundary_known
            else "observed_internal_window_only"
        ),
        "full_contract_history_confirmed": boundary_known,
        "reasons": reasons,
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
        raise ValueError(
            "notional_usdt must be finite and >= 0"
        )

    if not math.isfinite(rate):
        raise ValueError("funding_rate must be finite")

    if direction == "LONG":
        sign = -1.0
    elif direction == "SHORT":
        sign = 1.0
    else:
        raise ValueError("side must be LONG or SHORT")

    return sign * notional * rate
