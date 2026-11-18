
"""Read-only, fail-closed snapshot of Toobit V2 conditional/TP-SL orders.

This module only calls the documented GET /api/v2/futures/open-algo-orders
endpoint. It reads STOP and STOP_PROFIT_LOSS separately because Toobit
excludes TP/SL rows when stopCategory is omitted.

A snapshot is accepted only when both categories return fewer than the
requested limit and two complete scans are equivalent.

No order is created, amended, or canceled by this module.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

ENDPOINT = "/api/v2/futures/open-algo-orders"
STOP_CATEGORIES = ("STOP", "STOP_PROFIT_LOSS")
ACTIVE_STATUS = "ORDER_NEW"

VALID_TYPES = {
    "STOP",
    "STOP_MARKET",
    "STOP_PROFIT_LOSS",
    "STOP_PROFIT_LOSS_MARKET",
}

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+-SWAP-USDT$")


class AlgoOrderSnapshotError(RuntimeError):
    """Raised when a stable, bounded algo-order snapshot cannot be proven."""


def _positive_or_zero(value, field):
    if isinstance(value, bool) or value is None:
        raise AlgoOrderSnapshotError(
            f"{field}_missing_or_invalid"
        )

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AlgoOrderSnapshotError(
            f"{field}_not_numeric"
        ) from None

    if not number.is_finite() or number < 0:
        raise AlgoOrderSnapshotError(
            f"{field}_out_of_range"
        )

    return number


def _order_id(row):
    value = row.get("orderId")

    if value is None or isinstance(value, bool):
        raise AlgoOrderSnapshotError(
            "algo_order_id_missing"
        )

    text = str(value)

    if not re.fullmatch(r"[0-9]+", text) or int(text) <= 0:
        raise AlgoOrderSnapshotError(
            "algo_order_id_invalid"
        )

    return text


def _validate_row(row, category, symbol=None):
    if not isinstance(row, dict):
        raise AlgoOrderSnapshotError(
            "algo_order_row_not_object"
        )

    order_id = _order_id(row)

    row_symbol = row.get("symbol")

    if (
        not isinstance(row_symbol, str)
        or not SYMBOL_PATTERN.fullmatch(row_symbol)
    ):
        raise AlgoOrderSnapshotError(
            "algo_order_symbol_invalid"
        )

    if symbol is not None and row_symbol != symbol:
        raise AlgoOrderSnapshotError(
            "algo_order_symbol_mismatch"
        )

    side = row.get("side")
    position_side = row.get("positionSide")

    if (
        side not in ("BUY", "SELL")
        or position_side not in ("LONG", "SHORT")
    ):
        raise AlgoOrderSnapshotError(
            "algo_order_side_invalid"
        )

    order_type = row.get("type")

    if order_type not in VALID_TYPES:
        raise AlgoOrderSnapshotError(
            "algo_order_type_invalid"
        )

    if category == "STOP" and order_type not in (
        "STOP",
        "STOP_MARKET",
    ):
        raise AlgoOrderSnapshotError(
            "algo_order_stop_category_mismatch"
        )

    if category == "STOP_PROFIT_LOSS" and order_type not in (
        "STOP_PROFIT_LOSS",
        "STOP_PROFIT_LOSS_MARKET",
    ):
        raise AlgoOrderSnapshotError(
            "algo_order_tpsl_category_mismatch"
        )

    if row.get("status") != ACTIVE_STATUS:
        raise AlgoOrderSnapshotError(
            "algo_order_not_active_or_status_unknown"
        )

    _positive_or_zero(
        row.get("origQty"),
        "algo_order_orig_qty",
    )

    _positive_or_zero(
        row.get("executedQty"),
        "algo_order_executed_qty",
    )

    stop_price = _positive_or_zero(
        row.get("stopPrice"),
        "algo_order_stop_price",
    )

    if category == "STOP" and stop_price <= 0:
        raise AlgoOrderSnapshotError(
            "algo_order_stop_price_not_positive"
        )

    if category == "STOP_PROFIT_LOSS":
        stop_order_type = row.get("stopOrderType")

        if stop_order_type not in (
            "TAKE_PROFIT",
            "STOP_LOSS",
        ):
            raise AlgoOrderSnapshotError(
                "algo_order_stop_order_type_invalid"
            )

        stop_type = row.get("stopType")

        if stop_type not in (
            "FIXED_STOP",
            "TRAILING_STOP",
        ):
            raise AlgoOrderSnapshotError(
                "algo_order_stop_type_invalid"
            )

        if stop_type == "FIXED_STOP" and stop_price <= 0:
            raise AlgoOrderSnapshotError(
                "fixed_tpsl_stop_price_not_positive"
            )

    return order_id


def _scan_category(
    client,
    category,
    *,
    symbol,
    limit,
):
    method = getattr(
        client,
        "_request_v2_get",
        None,
    )

    if not callable(method):
        raise AlgoOrderSnapshotError(
            "client_v2_get_method_unavailable"
        )

    params = {
        "category": "USDT",
        "stopCategory": category,
        "limit": limit,
    }

    if symbol is not None:
        params["symbol"] = symbol

    try:
        rows = method(
            ENDPOINT,
            params,
        )
    except Exception as exc:
        raise AlgoOrderSnapshotError(
            "algo_order_request_failed:"
            f"{type(exc).__name__}"
        ) from None

    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise AlgoOrderSnapshotError(
            "algo_order_response_not_list_of_objects"
        )

    # The published V2 reference identifies orderId as a cursor but
    # does not define enough cursor ordering/termination semantics
    # to safely infer a complete multi-page traversal.
    # Never silently accept a potentially truncated full page.
    if len(rows) >= limit:
        raise AlgoOrderSnapshotError(
            "algo_order_page_may_be_truncated"
        )

    seen = set()
    normalized = []

    for row in rows:
        order_id = _validate_row(
            row,
            category,
            symbol,
        )

        if order_id in seen:
            raise AlgoOrderSnapshotError(
                "duplicate_algo_order_id"
            )

        seen.add(order_id)
        normalized.append(dict(row))

    return sorted(
        normalized,
        key=lambda item: int(item["orderId"]),
    )


def _scan_all(
    client,
    *,
    symbol,
    limit,
):
    combined = []
    seen = set()

    for category in STOP_CATEGORIES:
        rows = _scan_category(
            client,
            category,
            symbol=symbol,
            limit=limit,
        )

        for row in rows:
            order_id = str(row["orderId"])

            if order_id in seen:
                raise AlgoOrderSnapshotError(
                    "duplicate_algo_order_id_across_categories"
                )

            seen.add(order_id)
            combined.append(row)

    return sorted(
        combined,
        key=lambda item: int(item["orderId"]),
    )


def fetch_stable_v2_algo_order_snapshot(
    client,
    *,
    symbol=None,
    limit=1000,
):
    """Return a stable, bounded V2 algo-order snapshot or raise."""

    if client is None:
        raise ValueError("client is required")

    if symbol is not None:
        if (
            not isinstance(symbol, str)
            or not SYMBOL_PATTERN.fullmatch(symbol)
        ):
            raise ValueError(
                "symbol must be a valid USDT-M swap symbol"
            )

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 1000
    ):
        raise ValueError(
            "limit must be an integer from 1 to 1000"
        )

    first = _scan_all(
        client,
        symbol=symbol,
        limit=limit,
    )

    second = _scan_all(
        client,
        symbol=symbol,
        limit=limit,
    )

    if first != second:
        raise AlgoOrderSnapshotError(
            "algo_order_snapshot_changed_between_reads"
        )

    return {
        "orders": second,
        "stable": True,
        "complete_within_documented_page_bound": True,
        "categories_checked": list(STOP_CATEGORIES),
        "scans": 2,
        "read_only": True,
        "network_methods_used": ["GET"],
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
    }
