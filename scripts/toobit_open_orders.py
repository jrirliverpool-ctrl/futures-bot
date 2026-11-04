"""Fail-closed pagination for Toobit V1 futures open orders.

Read-only: this module never submits, amends, or cancels orders.
The V1 endpoint returns only the latest outstanding order when orderId
is omitted. Supplying orderId returns orders with smaller IDs.
"""
from __future__ import annotations

import re

ENDPOINT = "/api/v1/futures/openOrders"


class OpenOrdersReadError(RuntimeError):
    """The complete open-order set could not be proven."""


def _order_id(row):
    if not isinstance(row, dict):
        raise OpenOrdersReadError("open_orders_row_invalid")

    value = row.get("orderId")
    if isinstance(value, bool) or value is None:
        raise OpenOrdersReadError("open_orders_order_id_missing")

    text = str(value)
    if not re.fullmatch(r"[0-9]+", text):
        raise OpenOrdersReadError("open_orders_order_id_invalid")

    number = int(text)
    if number <= 0:
        raise OpenOrdersReadError("open_orders_order_id_invalid")

    return number


def _scan_once(client, *, symbol, order_type, limit, max_pages):
    if client is None or not callable(getattr(client, "_request", None)):
        raise OpenOrdersReadError("open_orders_client_invalid")

    params = {"limit": limit}

    if symbol is not None:
        params["symbol"] = symbol

    if order_type is not None:
        params["type"] = order_type

    cursor = None
    seen = {}
    result = []

    for page_index in range(max_pages):
        try:
            page = client._request("GET", ENDPOINT, dict(params))
        except Exception:
            raise OpenOrdersReadError("open_orders_request_failed") from None

        if isinstance(page, dict) and page.get("_transport"):
            raise OpenOrdersReadError("open_orders_transport_unknown")

        if not isinstance(page, list):
            raise OpenOrdersReadError("open_orders_response_not_list")

        if not all(isinstance(row, dict) for row in page):
            raise OpenOrdersReadError("open_orders_row_invalid")

        if not page:
            return sorted(
                result,
                key=lambda row: _order_id(row),
                reverse=True,
            )

        if len(page) > limit:
            raise OpenOrdersReadError("open_orders_page_exceeds_limit")

        page_ids = []

        for row in page:
            order_id = _order_id(row)

            if cursor is not None and order_id >= cursor:
                raise OpenOrdersReadError(
                    "open_orders_cursor_did_not_move_backwards"
                )

            if order_id in seen:
                raise OpenOrdersReadError("open_orders_duplicate_order_id")

            seen[order_id] = row
            page_ids.append(order_id)
            result.append(row)

        next_cursor = min(page_ids)

        if cursor is not None and next_cursor >= cursor:
            raise OpenOrdersReadError("open_orders_cursor_stalled")

        cursor = next_cursor
        params["orderId"] = cursor

        if page_index == max_pages - 1:
            raise OpenOrdersReadError("open_orders_page_limit_reached")

    raise OpenOrdersReadError("open_orders_scan_incomplete")


def fetch_all_open_orders(
    client,
    *,
    symbol=None,
    order_type=None,
    limit=1000,
    max_pages=100,
    verify_stable=True,
):
    """Fetch all open orders using the documented descending orderId cursor.

    The endpoint is scanned until it returns an empty page. A short page is
    NOT treated as the end because the initial request may return only the
    latest outstanding order.

    When verify_stable=True, a second full scan must match the first scan.
    """
    if symbol is not None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be non-empty text")

    if order_type is not None:
        if order_type not in ("LIMIT", "STOP", "STOP_PROFIT_LOSS"):
            raise ValueError("unsupported order_type")

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 1000
    ):
        raise ValueError("limit must be from 1 to 1000")

    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or not 1 <= max_pages <= 1000
    ):
        raise ValueError("max_pages must be from 1 to 1000")

    first = _scan_once(
        client,
        symbol=symbol,
        order_type=order_type,
        limit=limit,
        max_pages=max_pages,
    )

    if not verify_stable:
        return first

    second = _scan_once(
        client,
        symbol=symbol,
        order_type=order_type,
        limit=limit,
        max_pages=max_pages,
    )

    first_by_id = {_order_id(row): row for row in first}
    second_by_id = {_order_id(row): row for row in second}

    if first_by_id != second_by_id:
        raise OpenOrdersReadError("open_orders_snapshot_changed_between_scans")

    return second