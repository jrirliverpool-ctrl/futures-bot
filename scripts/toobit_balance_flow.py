
"""Read-only, fail-closed pagination and accounting for Toobit futures balance flow.

Uses the documented V1 USDT-M endpoint:
GET /api/v1/futures/balanceFlow

No order placement, amendment, cancellation, or account mutation.
The endpoint's queryable history is limited to the most recent 90 days.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation


ENDPOINT = "/api/v1/futures/balanceFlow"

# Documented flowType numeric codes.
FLOW_TYPES = {
    10: "FEE",
    28: "REALIZED_PNL",
    32: "FUNDING",
    700: "LIQUIDATION",
    701: "ADL",
}


class BalanceFlowError(RuntimeError):
    """Raised when a complete, valid flow snapshot cannot be proven."""


def _flow_id(row):
    if not isinstance(row, dict):
        raise BalanceFlowError("flow_row_not_object")

    value = row.get("id")

    if value is None or isinstance(value, bool):
        raise BalanceFlowError("flow_id_missing_or_invalid")

    text = str(value)

    if not text.isdigit():
        raise BalanceFlowError("flow_id_not_integer")

    parsed = int(text)

    if parsed <= 0:
        raise BalanceFlowError("flow_id_not_positive")

    return parsed


def _decimal(value, field, *, signed=False):
    if value is None or isinstance(value, bool):
        raise BalanceFlowError(f"{field}_missing_or_invalid")

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BalanceFlowError(f"{field}_not_numeric") from None

    if not number.is_finite():
        raise BalanceFlowError(f"{field}_not_finite")

    if not signed and number < 0:
        raise BalanceFlowError(f"{field}_negative")

    return number


def _flow_type_code(value):
    if isinstance(value, bool) or value is None:
        return None

    text = str(value).strip()

    if not text.isdigit():
        return None

    return int(text)


def normalize_flow_row(row):
    """Validate and normalize one futures balance-flow row."""

    flow_id = _flow_id(row)

    coin = row.get("coin")
    if not isinstance(coin, str) or not coin.strip():
        raise BalanceFlowError("flow_coin_missing")

    flow_type = row.get("flowType")
    if flow_type is None:
        raise BalanceFlowError("flow_type_missing")

    change = _decimal(
        row.get("change"),
        "flow_change",
        signed=True,
    )

    total = _decimal(
        row.get("total"),
        "flow_total",
    )

    created = row.get("created")
    if isinstance(created, bool) or created is None:
        raise BalanceFlowError("flow_created_missing")

    created_text = str(created)
    if not created_text.isdigit():
        raise BalanceFlowError("flow_created_not_integer")

    created_ms = int(created_text)
    if created_ms <= 0:
        raise BalanceFlowError("flow_created_not_positive")

    code = _flow_type_code(flow_type)

    return {
        "id": str(flow_id),
        "coin": coin.strip(),
        "symbol": (
            row.get("symbol")
            if isinstance(row.get("symbol"), str)
            else None
        ),
        "flow_type_raw": str(flow_type),
        "flow_type_code": code,
        "category": FLOW_TYPES.get(code, "UNKNOWN"),
        "change": str(change),
        "total": str(total),
        "created_ms": created_ms,
    }


def _scan_once(
    client,
    *,
    coin=None,
    start_time=None,
    end_time=None,
    page_size=1000,
    max_pages=100,
):
    if client is None or not callable(
        getattr(client, "_request", None)
    ):
        raise BalanceFlowError("balance_flow_client_invalid")

    params = {"limit": page_size}

    if coin is not None:
        if not isinstance(coin, str) or not coin.strip():
            raise ValueError("coin must be non-empty text")
        params["coin"] = coin.strip()

    for name, value in (
        ("startTime", start_time),
        ("endTime", end_time),
    ):
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(
                    f"{name} must be a non-negative integer"
                )
            params[name] = value

    if (
        start_time is not None
        and end_time is not None
        and end_time < start_time
    ):
        raise ValueError("end_time_before_start_time")

    result = []
    seen_ids = set()
    cursor = None

    for page_index in range(max_pages):
        try:
            page = client._request(
                "GET",
                ENDPOINT,
                dict(params),
            )
        except Exception as exc:
            raise BalanceFlowError(
                f"balance_flow_request_failed:{type(exc).__name__}"
            ) from None

        if isinstance(page, dict) and page.get("_transport"):
            raise BalanceFlowError(
                "balance_flow_transport_unknown"
            )

        if not isinstance(page, list):
            raise BalanceFlowError(
                "balance_flow_response_not_list"
            )

        if not all(isinstance(row, dict) for row in page):
            raise BalanceFlowError(
                "balance_flow_contains_invalid_rows"
            )

        if len(page) > page_size:
            raise BalanceFlowError(
                "balance_flow_page_exceeds_limit"
            )

        if not page:
            return result

        page_ids = []

        for raw in page:
            normalized = normalize_flow_row(raw)
            flow_id = int(normalized["id"])

            if flow_id in seen_ids:
                raise BalanceFlowError(
                    "balance_flow_duplicate_id"
                )

            if cursor is not None and flow_id >= cursor:
                raise BalanceFlowError(
                    "balance_flow_cursor_not_decreasing"
                )

            if page_ids and flow_id >= page_ids[-1]:
                raise BalanceFlowError(
                    "balance_flow_page_not_descending"
                )

            seen_ids.add(flow_id)
            page_ids.append(flow_id)
            result.append(normalized)

        # The documented cursor returns records with IDs below fromId.
        # A short page proves the end of this query direction.
        if len(page) < page_size:
            return result

        next_cursor = min(page_ids)

        if cursor is not None and next_cursor >= cursor:
            raise BalanceFlowError(
                "balance_flow_cursor_stalled"
            )

        cursor = next_cursor
        params["fromId"] = cursor

        if page_index == max_pages - 1:
            raise BalanceFlowError(
                "balance_flow_page_limit_reached"
            )

    raise BalanceFlowError("balance_flow_scan_incomplete")


def fetch_all_futures_balance_flow(
    client,
    *,
    coin=None,
    start_time=None,
    end_time=None,
    page_size=1000,
    max_pages=100,
    verify_stable=True,
):
    """Fetch a complete query-window snapshot.

    When verify_stable=True, two complete scans must match.
    This proves repeatability between scans, not permanent immutability.
    """

    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError("page_size must be from 1 to 1000")

    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or max_pages < 1
    ):
        raise ValueError("max_pages must be positive")

    first = _scan_once(
        client,
        coin=coin,
        start_time=start_time,
        end_time=end_time,
        page_size=page_size,
        max_pages=max_pages,
    )

    if not verify_stable:
        return first

    second = _scan_once(
        client,
        coin=coin,
        start_time=start_time,
        end_time=end_time,
        page_size=page_size,
        max_pages=max_pages,
    )

    first_by_id = {row["id"]: row for row in first}
    second_by_id = {row["id"]: row for row in second}

    if first_by_id != second_by_id:
        raise BalanceFlowError(
            "balance_flow_snapshot_changed_between_scans"
        )

    return second


def summarize_balance_flow(rows):
    """Aggregate known flow categories without guessing unknown types."""

    if not isinstance(rows, list):
        raise BalanceFlowError("balance_flow_rows_must_be_list")

    totals = defaultdict(Decimal)
    counts = defaultdict(int)
    unknown_types = defaultdict(int)
    coin_totals = defaultdict(Decimal)

    seen_ids = set()

    for row in rows:
        normalized = normalize_flow_row({
            "id": row.get("id"),
            "coin": row.get("coin"),
            "symbol": row.get("symbol"),
            "flowType": row.get("flow_type_raw"),
            "change": row.get("change"),
            "total": row.get("total"),
            "created": row.get("created_ms"),
        })

        flow_id = normalized["id"]
        if flow_id in seen_ids:
            raise BalanceFlowError(
                "balance_flow_duplicate_id_in_summary"
            )
        seen_ids.add(flow_id)

        coin = normalized["coin"]
        category = normalized["category"]
        change = Decimal(normalized["change"])

        counts[category] += 1
        coin_totals[coin] += change

        if category == "UNKNOWN":
            unknown_types[normalized["flow_type_raw"]] += 1
        else:
            totals[(coin, category)] += change

    by_coin = {}

    all_coins = sorted(
        set(coin_totals)
        | {coin for coin, _ in totals}
    )

    for coin in all_coins:
        by_coin[coin] = {
            category: str(totals[(coin, category)])
            for category in (
                "FEE",
                "FUNDING",
                "REALIZED_PNL",
                "LIQUIDATION",
                "ADL",
            )
        }
        by_coin[coin]["all_flow_changes"] = str(
            coin_totals[coin]
        )

    return {
        "row_count": len(rows),
        "category_counts": {
            key: counts[key]
            for key in sorted(counts)
        },
        "category_change_by_coin": by_coin,
        "unknown_flow_types": dict(
            sorted(unknown_types.items())
        ),
        "unknown_flow_type_count": sum(
            unknown_types.values()
        ),
        "known_flow_categories_complete": (
            sum(unknown_types.values()) == 0
        ),
    }
