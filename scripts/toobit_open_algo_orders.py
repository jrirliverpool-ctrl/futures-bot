"""Fail-closed V2 pagination for Toobit conditional and protection orders."""
from __future__ import annotations

import re

STOP_CATEGORIES = ("STOP", "STOP_PROFIT_LOSS")


class OpenAlgoOrdersReadError(RuntimeError):
    """A complete stable conditional-order snapshot could not be proven."""


def _order_id(row):
    value = row.get("orderId") if isinstance(row, dict) else None
    if isinstance(value, bool) or value is None:
        raise OpenAlgoOrdersReadError("algo_order_id_missing")
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or int(text) <= 0:
        raise OpenAlgoOrdersReadError("algo_order_id_invalid")
    return int(text)


def _scan_category(client, category, *, symbol, limit, max_pages):
    cursor = None
    rows_by_id = {}
    for page_index in range(max_pages):
        try:
            page = client.get_open_algo_orders_v2(
                stop_category=category,
                symbol=symbol,
                order_id=cursor,
                limit=limit,
            )
        except Exception:
            raise OpenAlgoOrdersReadError("algo_orders_request_failed") from None
        if not isinstance(page, list) or not all(isinstance(x, dict) for x in page):
            raise OpenAlgoOrdersReadError("algo_orders_response_invalid")
        if len(page) > limit:
            raise OpenAlgoOrdersReadError("algo_orders_page_exceeds_limit")
        if not page:
            return sorted(rows_by_id.values(), key=_order_id, reverse=True)

        ids = []
        for row in page:
            order_id = _order_id(row)
            if cursor is not None and order_id >= cursor:
                raise OpenAlgoOrdersReadError("algo_orders_cursor_not_decreasing")
            if order_id in rows_by_id:
                raise OpenAlgoOrdersReadError("algo_orders_duplicate_order_id")
            if row.get("symbol") is None or row.get("positionSide") not in ("LONG", "SHORT"):
                raise OpenAlgoOrdersReadError("algo_orders_row_missing_position_identity")
            rows_by_id[order_id] = row
            ids.append(order_id)
        cursor = min(ids)
        if page_index == max_pages - 1:
            raise OpenAlgoOrdersReadError("algo_orders_page_limit_reached")
    raise OpenAlgoOrdersReadError("algo_orders_scan_incomplete")


def fetch_all_open_algo_orders(
    client, *, symbol=None, limit=1000, max_pages=100, verify_stable=True
):
    """Fetch both documented categories; omission would hide TP/SL orders."""
    if symbol is not None and (
        not isinstance(symbol, str) or not symbol.strip()
    ):
        raise ValueError("symbol must be non-empty text")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be from 1 to 1000")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be from 1 to 1000")

    def scan():
        combined = {}
        for category in STOP_CATEGORIES:
            rows = _scan_category(
                client, category, symbol=symbol, limit=limit, max_pages=max_pages
            )
            for row in rows:
                key = _order_id(row)
                if key in combined:
                    raise OpenAlgoOrdersReadError("algo_order_id_cross_category_duplicate")
                combined[key] = row
        return combined

    first = scan()
    if verify_stable:
        second = scan()
        if first != second:
            raise OpenAlgoOrdersReadError("algo_orders_snapshot_changed_between_scans")
        first = second
    return sorted(first.values(), key=_order_id, reverse=True)
