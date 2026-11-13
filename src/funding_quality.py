
"""Funding history quality analysis and normalized backtest records.

No network access, no fabricated settlement records, and no zero-fill
for missing funding rates.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable
import math
import re

SCHEMA_VERSION = "1.0"
DEFAULT_PERIOD_HOURS = 8.0
TIMESTAMP_TOLERANCE_MS = 60_000


def parse_period_hours(value: Any) -> float | None:
    """Parse explicit exchange period metadata such as 1H, 4H or 8H."""
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
    return hours if math.isfinite(hours) and hours > 0 else None


def normalize_timestamp_ms(value: Any) -> int | None:
    """Normalize seconds or milliseconds to Unix milliseconds."""
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


def _period_distribution(records: list[dict]) -> Counter:
    result = Counter()

    for record in records:
        period = parse_period_hours(record.get("period"))
        if period is not None:
            result[str(period)] += 1

    return result


def _select_period(
    distribution: Counter,
    configured_period_hours: float | None,
) -> tuple[float | None, str]:
    """Prefer exchange metadata over timestamp-based inference."""
    if distribution:
        winner, count = distribution.most_common(1)[0]
        total = sum(distribution.values())

        if count / total >= 0.80:
            return float(winner), "exchange_metadata"

        return None, "mixed_exchange_metadata"

    if configured_period_hours is not None:
        if configured_period_hours > 0:
            return float(configured_period_hours), "configured_fallback"

    return DEFAULT_PERIOD_HOURS, "default_fallback"


def analyze_funding_records(
    records: Iterable[dict[str, Any]],
    *,
    configured_period_hours: float | None = None,
) -> dict[str, Any]:
    """Analyze records while retaining all valid unique settlements."""

    raw = list(records)
    valid = []
    invalid_timestamps = 0
    invalid_rates = 0
    invalid_records = 0

    for source in raw:
        if not isinstance(source, dict):
            invalid_records += 1
            continue

        timestamp = normalize_timestamp_ms(
            source.get("settleTime")
        )

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

    # Keep one canonical record per settlement timestamp.
    # Never discard the entire symbol because of isolated duplicates.
    by_timestamp: dict[int, dict] = {}
    duplicate_timestamps = []

    for item in valid:
        ts = item["settle_time_ms"]

        if ts in by_timestamp:
            duplicate_timestamps.append(ts)
            continue

        by_timestamp[ts] = item

    normalized = [
        by_timestamp[ts]
        for ts in sorted(by_timestamp)
    ]

    distribution = _period_distribution(raw)
    period_hours, period_source = _select_period(
        distribution,
        configured_period_hours,
    )

    missing_expected = 0
    irregular_gaps = []
    normal_intervals = 0
    gap_details = []

    if period_hours is not None:
        cadence_ms = period_hours * 3_600_000

        for previous, current in zip(
            normalized,
            normalized[1:],
        ):
            delta_ms = (
                current["settle_time_ms"]
                - previous["settle_time_ms"]
            )
            delta_hours = delta_ms / 3_600_000
            multiples = delta_ms / cadence_ms
            nearest = round(multiples)

            aligned = (
                nearest >= 1
                and abs(
                    delta_ms - nearest * cadence_ms
                ) <= TIMESTAMP_TOLERANCE_MS
            )

            if aligned:
                normal_intervals += 1
                missing = max(0, nearest - 1)
                missing_expected += missing

                if missing:
                    gap_details.append({
                        "from_utc": previous["settle_time_utc"],
                        "to_utc": current["settle_time_utc"],
                        "elapsed_hours": round(delta_hours, 4),
                        "expected_missing": missing,
                        "classification": "missing_settlements",
                    })
            else:
                irregular_gaps.append({
                    "from_utc": previous["settle_time_utc"],
                    "to_utc": current["settle_time_utc"],
                    "elapsed_hours": round(delta_hours, 4),
                    "classification": "irregular_or_schedule_shift",
                })

    unique_count = len(normalized)

    expected_scored = unique_count + missing_expected
    coverage = (
        unique_count / expected_scored * 100
        if expected_scored
        else None
    )

    rates = [item["funding_rate"] for item in normalized]

    period_metadata_mixed = period_source == (
        "mixed_exchange_metadata"
    )

    has_gaps = missing_expected > 0
    has_irregular = bool(irregular_gaps)

    if not normalized:
        status = "no_valid_data"
    elif period_metadata_mixed:
        status = "period_metadata_conflict"
    elif has_gaps or has_irregular:
        status = "usable_with_gaps"
    else:
        status = "complete_on_observed_schedule"

    return {
        "schema_version": SCHEMA_VERSION,
        "record_counts": {
            "raw": len(raw),
            "valid": len(valid),
            "unique_settlements": unique_count,
            "duplicate_timestamps": len(duplicate_timestamps),
            "invalid_timestamps": invalid_timestamps,
            "invalid_rates": invalid_rates,
            "invalid_records": invalid_records,
        },
        "period": {
            "hours": period_hours,
            "source": period_source,
            "metadata_distribution": dict(distribution),
        },
        "coverage": {
            "percentage_scored": (
                round(coverage, 6)
                if coverage is not None else None
            ),
            "missing_expected_settlements": missing_expected,
            "scored_intervals": normal_intervals,
            "irregular_intervals": len(irregular_gaps),
            "coverage_is_complete": (
                coverage == 100.0
                and not has_irregular
                and not period_metadata_mixed
            ),
        },
        "gaps": {
            "missing_settlements": gap_details,
            "irregular_intervals": irregular_gaps,
        },
        "rates": {
            "count": len(rates),
            "min": min(rates) if rates else None,
            "max": max(rates) if rates else None,
            "mean": (
                sum(rates) / len(rates)
                if rates else None
            ),
        },
        "status": status,
        "backtest_records": normalized,
    }


def funding_cashflow_usdt(
    notional_usdt: float,
    side: str,
    funding_rate: float,
) -> float:
    """Positive means received; negative means paid.

    Positive funding rate:
      LONG pays.
      SHORT receives.
    """
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
