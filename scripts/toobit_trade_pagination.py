
"""Fail-closed pagination for Toobit V2 user trades.

Read-only only. Never submits, amends, or cancels orders.
A result is returned only when pagination reaches a short or empty page.
"""

from decimal import Decimal, InvalidOperation


class TradePaginationError(RuntimeError):
    """Raised when complete, ordered trade history cannot be proven."""


def _numeric_trade_id(row):
    value = row.get("id")

    if value is None or isinstance(value, bool):
        raise TradePaginationError("trade_id_missing_or_invalid")

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise TradePaginationError("trade_id_not_numeric") from None

    if not parsed.is_finite() or parsed < 0:
        raise TradePaginationError("trade_id_not_finite_nonnegative")

    if parsed != parsed.to_integral_value():
        raise TradePaginationError("trade_id_not_integer")

    return int(parsed)


def fetch_complete_user_trades_v2(
    client,
    symbol,
    start_time=None,
    end_time=None,
    page_size=1000,
    max_pages=100,
    initial_from_id=None,
):
    """Fetch all pages or raise; never return a potentially partial history.

    Cursor progression uses last_trade_id + 1. IDs must be numeric and
    strictly increasing. Duplicate IDs, malformed rows, unexpected
    symbols, cursor stalls and page-limit exhaustion all fail closed.
    """

    if client is None:
        raise ValueError("client is required")

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol is required")

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
        raise ValueError("max_pages must be a positive integer")

    if initial_from_id is not None and (
        isinstance(initial_from_id, bool)
        or not isinstance(initial_from_id, int)
        or initial_from_id < 0
    ):
        raise ValueError("initial_from_id must be non-negative")

    method = getattr(client, "get_user_trades_v2", None)
    if not callable(method):
        raise TradePaginationError("v2_user_trades_method_unavailable")

    collected = []
    seen_ids = set()
    cursor = initial_from_id
    previous_id = None

    for page_number in range(1, max_pages + 1):
        rows = method(
            symbol,
            start_time=start_time,
            end_time=end_time,
            limit=page_size,
            from_id=cursor,
        )

        if not isinstance(rows, list):
            raise TradePaginationError("page_is_not_a_list")

        if not all(isinstance(row, dict) for row in rows):
            raise TradePaginationError("page_contains_non_object_row")

        if len(rows) > page_size:
            raise TradePaginationError("page_exceeds_requested_limit")

        if not rows:
            return collected

        page_ids = []

        for row in rows:
            if row.get("symbol") != symbol:
                raise TradePaginationError("trade_symbol_mismatch")

            trade_id = _numeric_trade_id(row)

            if trade_id in seen_ids:
                raise TradePaginationError("duplicate_trade_id")

            if previous_id is not None and trade_id <= previous_id:
                raise TradePaginationError("trade_ids_not_strictly_increasing")

            seen_ids.add(trade_id)
            page_ids.append(trade_id)
            previous_id = trade_id
            collected.append(row)

        # A short page proves the end of this query window.
        if len(rows) < page_size:
            return collected

        # A full page may have more records. Advance strictly beyond
        # the final ID; never retry the same cursor.
        next_cursor = page_ids[-1] + 1

        if cursor is not None and next_cursor <= cursor:
            raise TradePaginationError("pagination_cursor_did_not_advance")

        cursor = next_cursor

    # Reaching the page cap is not proof of completeness.
    raise TradePaginationError("pagination_page_limit_reached")
