
"""Fail-closed reconciliation of Toobit V2 protective algo orders.

Read-only verification only. Never creates, amends, or cancels orders.

Important Toobit semantics:
- TP/SL origQty=0 means full available position quantity at trigger time.
- origQty is configured quantity, not remaining position quantity.
- open-algo-orders returns effective, untriggered orders.
- executedQty is not required in the documented open-algo response.
- ORDER_FILLED means triggered, not necessarily fully executed.
"""
from decimal import Decimal, InvalidOperation
import re


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+-SWAP-USDT$")
EXPECTED_CATEGORIES = {"STOP", "STOP_PROFIT_LOSS"}


class ProtectionReconciliationError(RuntimeError):
    """Raised when an algo-order snapshot cannot be trusted."""


def _decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}_missing_or_invalid")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field}_not_numeric") from None

    if not result.is_finite():
        raise ValueError(f"{field}_not_finite")

    return result


def _nonnegative(value, field):
    result = _decimal(value, field)
    if result < 0:
        raise ValueError(f"{field}_negative")
    return result


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ProtectionReconciliationError("algo_snapshot_not_object")

    if snapshot.get("stable") is not True:
        raise ProtectionReconciliationError("algo_snapshot_not_stable")

    if snapshot.get("read_only") is not True:
        raise ProtectionReconciliationError("algo_snapshot_not_read_only")

    if snapshot.get("complete_within_documented_page_bound") is not True:
        raise ProtectionReconciliationError(
            "algo_snapshot_page_completeness_unproven"
        )

    categories = snapshot.get("categories_checked")
    if not isinstance(categories, (list, tuple, set)):
        raise ProtectionReconciliationError(
            "algo_snapshot_categories_invalid"
        )

    if set(categories) != EXPECTED_CATEGORIES:
        raise ProtectionReconciliationError(
            "algo_snapshot_categories_incomplete"
        )

    if snapshot.get("scans") != 2:
        raise ProtectionReconciliationError(
            "algo_snapshot_scan_count_invalid"
        )

    for field in (
        "orders_submitted",
        "orders_amended",
        "orders_canceled",
    ):
        if snapshot.get(field) != 0:
            raise ProtectionReconciliationError(
                "algo_snapshot_not_read_only"
            )

    if snapshot.get("network_methods_used") != ["GET"]:
        raise ProtectionReconciliationError(
            "algo_snapshot_network_methods_invalid"
        )

    orders = snapshot.get("orders")
    if not isinstance(orders, list):
        raise ProtectionReconciliationError(
            "algo_snapshot_orders_not_list"
        )

    if not all(isinstance(row, dict) for row in orders):
        raise ProtectionReconciliationError(
            "algo_snapshot_contains_invalid_rows"
        )

    return orders


def _normalize_positions(exchange_positions, configured_symbols):
    if not isinstance(exchange_positions, list):
        raise ValueError("exchange_positions_not_list")

    if not isinstance(configured_symbols, (list, tuple)):
        raise ValueError("configured_symbols_not_list")

    configured = set()

    for symbol in configured_symbols:
        if (
            not isinstance(symbol, str)
            or not SYMBOL_PATTERN.fullmatch(symbol)
        ):
            raise ValueError("configured_symbol_invalid")

        if symbol in configured:
            raise ValueError("duplicate_configured_symbol")

        configured.add(symbol)

    active = {}

    for index, row in enumerate(exchange_positions):
        if not isinstance(row, dict):
            raise ValueError(f"position_row_invalid:{index}")

        symbol = row.get("symbol")
        side = row.get("side")

        if (
            not isinstance(symbol, str)
            or not SYMBOL_PATTERN.fullmatch(symbol)
        ):
            raise ValueError(f"position_symbol_invalid:{index}")

        if side not in ("LONG", "SHORT"):
            raise ValueError(f"position_side_invalid:{index}")

        quantity = _nonnegative(
            row.get("position"),
            f"position_quantity_{index}",
        )

        if quantity == 0:
            continue

        if symbol not in configured:
            raise ValueError(
                f"active_position_outside_universe:{symbol}"
            )

        available = None
        if "available" in row:
            available = _nonnegative(
                row["available"],
                f"position_available_{index}",
            )

            if available > quantity:
                raise ValueError(
                    f"position_available_exceeds_position:{symbol}:{side}"
                )

        key = (symbol, side)

        if key in active:
            raise ValueError(
                f"duplicate_active_position:{symbol}:{side}"
            )

        active[key] = {
            "position_qty": quantity,
            "available_qty": available,
        }

    return active


def _validate_stop_loss_order(order):
    if order.get("status") != "ORDER_NEW":
        return None

    if order.get("stopOrderType") != "STOP_LOSS":
        return None

    stop_type = order.get("stopType")
    if stop_type not in ("FIXED_STOP", "TRAILING_STOP"):
        raise ValueError("stop_loss_type_invalid")

    if order.get("side") not in ("BUY", "SELL"):
        raise ValueError("stop_loss_side_invalid")

    if order.get("positionSide") not in ("LONG", "SHORT"):
        raise ValueError("stop_loss_position_side_invalid")

    symbol = order.get("symbol")
    if (
        not isinstance(symbol, str)
        or not SYMBOL_PATTERN.fullmatch(symbol)
    ):
        raise ValueError("stop_loss_symbol_invalid")

    order_id = order.get("orderId")
    if (
        order_id is None
        or isinstance(order_id, bool)
        or not str(order_id).isdigit()
        or int(order_id) <= 0
    ):
        raise ValueError("stop_loss_order_id_invalid")

    original = _nonnegative(
        order.get("origQty"),
        "stop_loss_orig_qty",
    )

    stop_price = _nonnegative(
        order.get("stopPrice"),
        "stop_loss_stop_price",
    )

    if stop_type == "FIXED_STOP":
        if stop_price <= 0:
            raise ValueError(
                "fixed_stop_loss_trigger_price_not_positive"
            )

    else:
        # Trailing stops can have stopPrice=0. Their callback
        # configuration must nevertheless be valid.
        fallback_type = order.get("fallbackType")
        if fallback_type not in ("RATE", "PRICE"):
            raise ValueError(
                "trailing_stop_fallback_type_invalid"
            )

        fallback_quantity = _decimal(
            order.get("fallbackQuantity"),
            "trailing_stop_fallback_quantity",
        )

        if fallback_quantity <= 0:
            raise ValueError(
                "trailing_stop_fallback_quantity_not_positive"
            )

        if "activePrice" in order:
            active_price = _nonnegative(
                order["activePrice"],
                "trailing_stop_active_price",
            )
            # Zero is treated as an unset optional activation price.

        if "activeStatus" in order:
            active_status = order["activeStatus"]
            if (
                isinstance(active_status, bool)
                or active_status not in (0, 1, "0", "1")
            ):
                raise ValueError(
                    "trailing_stop_active_status_invalid"
                )

    # The documented open-algo response does not require executedQty.
    # Since this row is ORDER_NEW, a supplied nonzero executedQty is
    # inconsistent with the expected untriggered-order snapshot.
    if "executedQty" in order:
        executed = _nonnegative(
            order["executedQty"],
            "stop_loss_executed_qty",
        )
        if executed != 0:
            raise ValueError(
                "active_algo_order_has_nonzero_executed_qty"
            )

    return {
        "order_id": str(order_id),
        "symbol": symbol,
        "side": order["side"],
        "position_side": order["positionSide"],
        "configured_qty": original,
        "stop_price": stop_price,
        "stop_type": stop_type,
    }


def reconcile_protective_orders(
    exchange_positions,
    algo_snapshot,
    configured_symbols,
    qty_tolerance="0.00000001",
):
    """Verify one unambiguous stop-loss covering every active position."""

    try:
        tolerance = _decimal(qty_tolerance, "qty_tolerance")

        if tolerance < 0:
            raise ValueError("qty_tolerance_negative")

        positions = _normalize_positions(
            exchange_positions,
            configured_symbols,
        )

        orders = _validate_snapshot(algo_snapshot)

        valid_stops = []

        for index, order in enumerate(orders):
            if not isinstance(order, dict):
                raise ValueError(f"algo_order_invalid:{index}")

            if order.get("stopOrderType") != "STOP_LOSS":
                continue

            normalized = _validate_stop_loss_order(order)

            if normalized is not None:
                valid_stops.append(normalized)

        reports = {}
        all_ok = True

        for (symbol, position_side), position_data in positions.items():
            position_qty = position_data["position_qty"]
            available_qty = position_data["available_qty"]

            closing_side = (
                "SELL" if position_side == "LONG" else "BUY"
            )

            key = f"{symbol}:{position_side}"

            matches = [
                item
                for item in valid_stops
                if (
                    item["symbol"] == symbol
                    and item["position_side"] == position_side
                    and item["side"] == closing_side
                )
            ]

            if not matches:
                reports[key] = {
                    "ok": False,
                    "reason": "protective_stop_loss_missing",
                    "position_qty": str(position_qty),
                }
                all_ok = False
                continue

            if len(matches) != 1:
                reports[key] = {
                    "ok": False,
                    "reason": "multiple_matching_stop_losses",
                    "matching_order_ids": [
                        item["order_id"] for item in matches
                    ],
                }
                all_ok = False
                continue

            stop = matches[0]
            configured_qty = stop["configured_qty"]

            # Full-position TP/SL: origQty=0 means the exchange
            # closes the available quantity at trigger time.
            if configured_qty == 0:
                if available_qty is None:
                    reports[key] = {
                        "ok": False,
                        "reason": (
                            "full_position_stop_requires_available_quantity"
                        ),
                        "position_qty": str(position_qty),
                        "stop_order_id": stop["order_id"],
                    }
                    all_ok = False
                    continue

                if available_qty + tolerance < position_qty:
                    reports[key] = {
                        "ok": False,
                        "reason": (
                            "full_position_stop_available_quantity_insufficient"
                        ),
                        "position_qty": str(position_qty),
                        "available_qty": str(available_qty),
                        "stop_order_id": stop["order_id"],
                    }
                    all_ok = False
                    continue

                coverage_mode = "FULL_AVAILABLE_POSITION_AT_TRIGGER"
                coverage_qty = available_qty

            else:
                # origQty is the configured contract quantity.
                # Do not subtract executedQty to invent a remaining
                # quantity for an ORDER_NEW algo order.
                if configured_qty + tolerance < position_qty:
                    reports[key] = {
                        "ok": False,
                        "reason": "protective_stop_quantity_insufficient",
                        "position_qty": str(position_qty),
                        "stop_configured_qty": str(configured_qty),
                        "stop_order_id": stop["order_id"],
                    }
                    all_ok = False
                    continue

                if (
                    available_qty is not None
                    and available_qty + tolerance < position_qty
                ):
                    reports[key] = {
                        "ok": False,
                        "reason": "position_available_quantity_insufficient",
                        "position_qty": str(position_qty),
                        "available_qty": str(available_qty),
                        "stop_order_id": stop["order_id"],
                    }
                    all_ok = False
                    continue

                coverage_mode = "FIXED_CONFIGURED_QUANTITY"
                coverage_qty = configured_qty

            reports[key] = {
                "ok": True,
                "reason": "matching_stop_loss_verified",
                "position_qty": str(position_qty),
                "stop_configured_qty": str(configured_qty),
                "verified_coverage_qty": str(coverage_qty),
                "coverage_mode": coverage_mode,
                "stop_order_id": stop["order_id"],
                "stop_price": str(stop["stop_price"]),
                "stop_type": stop["stop_type"],
            }

        return {
            "ok": all_ok,
            "halt_trading": not all_ok,
            "positions_checked": len(positions),
            "protected_positions": sum(
                1 for item in reports.values()
                if item.get("ok") is True
            ),
            "unprotected_positions": sum(
                1 for item in reports.values()
                if item.get("ok") is not True
            ),
            "position_reports": reports,
            "read_only": True,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "state_mutated": False,
        }

    except (ValueError, ProtectionReconciliationError) as exc:
        return {
            "ok": False,
            "halt_trading": True,
            "reason": str(exc),
            "positions_checked": 0,
            "protected_positions": 0,
            "unprotected_positions": None,
            "position_reports": {},
            "read_only": True,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "state_mutated": False,
        }
