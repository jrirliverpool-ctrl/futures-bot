
"""Read-only, fail-closed pagination and accounting for Toobit futures balance flow.

Endpoint: GET /api/v1/futures/balanceFlow

The response uses flowTypeValue for numeric codes and flowType
for the human-readable flow name. The queryable history is limited
to the most recent 90 days.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation


ENDPOINT = "/api/v1/futures/balanceFlow"

FLOW_TYPES = {
    10: "FEE",
    28: "REALIZED_PNL",
    32: "FUNDING",
    700: "LIQUIDATION",
    701: "ADL",
}

FLOW_TYPE_NAMES = {
    "fee": 10,
    "fund fee": 32,
    "fund_fee": 32,
    "funding": 32,
    "realized pnl": 28,
    "realized_pnl": 28,
    "force liquidation": 700,
    "force_liquidation": 700,
    "liquidation": 700,
    "adl": 701,
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


def _parse_flow_type(row):
    raw_value = row.get("flowTypeValue")
    raw_name = row.get("flowType")

    numeric_code = None

    if raw_value is not None:
        if isinstance(raw_value, bool):
            raise BalanceFlowError("flow_type_value_invalid")

        text = str(raw_value).strip()

        if not text.isdigit():
            raise BalanceFlowError("flow_type_value_not_integer")

        numeric_code = int(text)

    name_code = None

    if raw_name is not None:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise BalanceFlowError("flow_type_name_invalid")

        normalized_name = raw_name.strip().lower()
        name_code = FLOW_TYPE_NAMES.get(normalized_name)

    if (
        numeric_code is not None
        and name_code is not None
        and numeric_code != name_code
    ):
        raise BalanceFlowError("flow_type_value_name_mismatch")

    code = (
        numeric_code
        if numeric_code is not None
        else name_code
    )

    if code is None:
        category = "UNKNOWN"
    else:
        category = FLOW_TYPES.get(code, "UNKNOWN")

    if raw_name is None and raw_value is None:
        raise BalanceFlowError("flow_type_missing")

    return code, category, (
        str(raw_name) if raw_name is not None
        else str(raw_value)
    )


def normalize_flow_row(row):
    """Validate and normalize one Toobit futures balance-flow row."""

    flow_id = _flow_id(row)

    coin = row.get("coin")

    if not isinstance(coin, str) or not coin.strip():
        raise BalanceFlowError("flow_coin_missing")

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

    code, category, raw_type = _parse_flow_type(row)

    symbol = row.get("symbol")

    if symbol is not None and (
        not isinstance(symbol, str) or not symbol.strip()
    ):
        raise BalanceFlowError("flow_symbol_invalid")

    return {
        "id": str(flow_id),
        "coin": coin.strip(),
        "symbol": symbol.strip() if symbol else None,
        "flow_type_raw": raw_type,
        "flow_type_value": code,
        "category": category,
        "change": str(change),
        "total": str(total),
        "created_ms": created_ms,
    }


def _scan_once(
    client,
    *,
    symbol=None,
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

    if symbol is not None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be non-empty text")
        params["symbol"] = symbol.strip()

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

        if len(page) < page_size:
            return result

        # For descending pagination, the minimum ID of this page
        # is passed as fromId to retrieve earlier records.
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
    symbol=None,
    start_time=None,
    end_time=None,
    page_size=1000,
    max_pages=100,
    verify_stable=True,
):
    """Fetch a complete query-window snapshot.

    Two matching scans are required by default.
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
        symbol=symbol,
        start_time=start_time,
        end_time=end_time,
        page_size=page_size,
        max_pages=max_pages,
    )

    if not verify_stable:
        return first

    second = _scan_once(
        client,
        symbol=symbol,
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
        if not isinstance(row, dict):
            raise BalanceFlowError("summary_row_not_object")

        normalized = normalize_flow_row({
            "id": row.get("id"),
            "coin": row.get("coin"),
            "symbol": row.get("symbol"),
            "flowType": row.get("flow_type_raw"),
            "flowTypeValue": row.get("flow_type_value"),
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
