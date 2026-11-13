
"""Read-only Toobit funding history probe with explicit backtest eligibility."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.funding_quality import (
    DEFAULT_MIN_INTERNAL_COVERAGE,
    analyze_funding_records,
    assess_backtest_eligibility,
)

BASE_URL = "https://api.toobit.com"
FUNDING_PATH = "/api/v1/futures/historyFundingRate"

DEFAULT_SYMBOL = "BTC-SWAP-USDT"
DEFAULT_LIMIT = 100
DEFAULT_MAX_PAGES = 5


def extract_item_id(item):
    if not isinstance(item, dict):
        return None

    for key in ("id", "fundingId", "rateId", "settleId"):
        value = item.get(key)

        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return None

    return None


def extract_items(payload):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("data", "result", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value

    raise ValueError("Unexpected API response structure")


def fetch_page(
    session,
    url,
    symbol,
    cursor=None,
    limit=DEFAULT_LIMIT,
    timeout=15,
):
    params = {
        "symbol": symbol,
        "limit": limit,
    }

    if cursor is not None:
        params["fromId"] = cursor

    try:
        response = session.get(
            url,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()

        return {
            "ok": True,
            "http_status": response.status_code,
            "request_url": response.url,
            "items": extract_items(payload),
            "response_json_raw": payload,
            "error": None,
        }

    except Exception as exc:
        return {
            "ok": False,
            "http_status": getattr(
                getattr(exc, "response", None),
                "status_code",
                None,
            ),
            "request_url": None,
            "items": None,
            "response_json_raw": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def analyze_pagination(pages, ids, items):
    counts = Counter(
        value for value in ids if value is not None
    )

    duplicates = {
        str(key): count
        for key, count in counts.items()
        if count > 1
    }

    missing_ids = sum(
        value is None for value in ids
    )

    timestamps = []

    for item in items:
        if not isinstance(item, dict):
            continue

        result = analyze_funding_records([item])
        records = result["backtest_records"]

        if records:
            timestamps.append(
                records[0]["settle_time_ms"]
            )

    ascending = (
        timestamps == sorted(timestamps)
        if timestamps else None
    )

    descending = (
        timestamps == sorted(timestamps, reverse=True)
        if timestamps else None
    )

    verified = bool(
        len(pages) >= 2
        and not duplicates
        and missing_ids == 0
        and (ascending or descending)
        and all(page.get("ok") for page in pages)
    )

    return {
        "pages_fetched": len(pages),
        "duplicate_ids": duplicates,
        "missing_ids": missing_ids,
        "source_order_ascending": ascending,
        "source_order_descending": descending,
        "pagination_verified": verified,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only Toobit funding quality probe"
    )

    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--sleep-between", type=float, default=0.5)
    parser.add_argument("--configured-period-hours", type=float)

    parser.add_argument(
        "--min-internal-coverage",
        type=float,
        default=DEFAULT_MIN_INTERNAL_COVERAGE,
    )

    parser.add_argument("--out", required=True)

    args = parser.parse_args(argv)

    if args.limit < 1 or args.max_pages < 1:
        parser.error("limit and max-pages must be positive")

    if args.max_seconds <= 0 or args.sleep_between < 0:
        parser.error("invalid timing arguments")

    if not 0 <= args.min_internal_coverage <= 100:
        parser.error(
            "min-internal-coverage must be between 0 and 100"
        )

    url = BASE_URL + FUNDING_PATH
    deadline = time.monotonic() + args.max_seconds

    raw_items = []
    ids = []
    pages = []
    errors = []

    cursor = None
    endpoint_ok = False
    pagination_stopped_safely = True
    stop_reason = "max_pages_reached"

    session = requests.Session()

    try:
        for page_number in range(1, args.max_pages + 1):

            if time.monotonic() >= deadline:
                stop_reason = "max_seconds_reached"
                pagination_stopped_safely = False
                break

            result = fetch_page(
                session,
                url,
                args.symbol,
                cursor=cursor,
                limit=args.limit,
            )

            pages.append({
                "page": page_number,
                "http_status": result["http_status"],
                "request_url": result["request_url"],
                "items_returned": (
                    len(result["items"])
                    if result["items"] is not None
                    else None
                ),
                "error": result["error"],
                "ok": result["ok"],
                "response_json_raw": result["response_json_raw"],
            })

            if not result["ok"]:
                errors.append({
                    "page": page_number,
                    "error": result["error"],
                })
                stop_reason = "endpoint_error"
                pagination_stopped_safely = False
                break

            endpoint_ok = True
            items = result["items"]

            if not items:
                stop_reason = "empty_page"
                break

            for item in items:
                raw_items.append(item)
                ids.append(extract_item_id(item))

            if len(items) < args.limit:
                stop_reason = "last_page"
                break

            next_cursor = extract_item_id(items[-1])

            if next_cursor is None:
                stop_reason = "missing_cursor_id"
                pagination_stopped_safely = False
                break

            if next_cursor == cursor:
                stop_reason = "unchanged_cursor"
                pagination_stopped_safely = False
                break

            cursor = next_cursor

            if page_number < args.max_pages:
                time.sleep(args.sleep_between)

    finally:
        session.close()

    quality = analyze_funding_records(
        raw_items,
        configured_period_hours=args.configured_period_hours,
    )

    pagination = analyze_pagination(
        pages,
        ids,
        raw_items,
    )

    collection_complete = bool(
        stop_reason in {"last_page", "empty_page"}
        and not errors
        and pagination_stopped_safely
    )

    eligibility = assess_backtest_eligibility(
        quality,
        endpoint_ok=endpoint_ok,
        collection_complete=collection_complete,
        min_internal_coverage=args.min_internal_coverage,
    )

    report = {
        "probe_version": "3.0",
        "symbol": args.symbol,
        "target_url": url,
        "fetched_at_iso": datetime.now(
            timezone.utc
        ).isoformat(),
        "endpoint_ok": endpoint_ok,
        "data_quality_usable": quality["data_quality_usable"],
        "backtest_eligible": eligibility["backtest_eligible"],
        "backtest_eligibility": eligibility,
        "collection_complete": collection_complete,
        "pagination": {
            **pagination,
            "stop_reason": stop_reason,
            "pagination_stopped_safely": pagination_stopped_safely,
        },
        "quality": quality,
        "raw_items": raw_items,
        "pages": pages,
        "errors": errors,
    }

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)

    output.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    print(json.dumps({
        "symbol": args.symbol,
        "endpoint_ok": endpoint_ok,
        "data_quality_usable": quality["data_quality_usable"],
        "backtest_eligible": eligibility["backtest_eligible"],
        "eligibility_reasons": eligibility["reasons"],
        "collection_complete": collection_complete,
        "records": quality["record_counts"]["unique_settlements"],
        "period": quality["period"],
        "coverage": quality["coverage"],
        "status": quality["status"],
        "stop_reason": stop_reason,
        "report": str(output),
    }, ensure_ascii=False, indent=2))

    # Ineligibility is an audit result, not a probe execution failure.
    # Only endpoint failure returns non-zero.
    return 0 if endpoint_ok else 1


if __name__ == "__main__":
    sys.exit(main())
