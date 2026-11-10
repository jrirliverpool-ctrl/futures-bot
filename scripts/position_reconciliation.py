
"""Read-only reconciliation of internal position expectations with Toobit v1 positions.

This module performs no network requests and never mutates trading state.
Any ambiguity returns ok=False / halt_trading=True.
"""
from decimal import Decimal, InvalidOperation


def _decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} missing or invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} is not numeric") from None
    if not result.is_finite():
        raise ValueError(f"{field} is not finite")
    return result


def reconcile_position_snapshot(
    symbol,
    expected_position,
    exchange_positions,
    qty_tolerance="0.00000001",
    price_tolerance="0.00000001",
):
    """Compare one internal position with a complete Toobit v1 positions list.

    expected_position is None for flat, otherwise:
      {"side": "LONG"|"SHORT", "quantity": positive, "avg_price": positive}
    """
    reasons = []

    if not isinstance(symbol, str) or not symbol.strip():
        return _failure("invalid_symbol")

    if not isinstance(exchange_positions, list):
        return _failure("exchange_positions_not_list")

    try:
        qty_tol = _decimal(qty_tolerance, "qty_tolerance")
        price_tol = _decimal(price_tolerance, "price_tolerance")
        if qty_tol < 0 or price_tol < 0:
            raise ValueError("tolerances must be non-negative")
    except ValueError as exc:
        return _failure(str(exc))

    expected = None
    if expected_position is not None:
        if not isinstance(expected_position, dict):
            return _failure("expected_position_not_dict")
        # Legacy single-side snapshots remain accepted during migration.
        if "side" in expected_position:
            expected_sides = {expected_position.get("side"): expected_position}
        else:
            expected_sides = expected_position
        expected = {}
        for side, record in expected_sides.items():
            if side not in ("LONG", "SHORT") or not isinstance(record, dict):
                return _failure("expected_position_side_invalid")
            try:
                quantity = _decimal(record.get("quantity"), "expected.quantity")
                avg_price = _decimal(record.get("avg_price"), "expected.avg_price")
            except ValueError as exc:
                return _failure(str(exc))
            if quantity <= 0 or avg_price <= 0:
                return _failure("expected_position_values_must_be_positive")
            expected[side] = {"quantity": quantity, "avg_price": avg_price}
        if not expected:
            return _failure("expected_position_side_invalid")

    active = []
    for index, row in enumerate(exchange_positions):
        if not isinstance(row, dict):
            reasons.append(f"position[{index}]_not_object")
            continue

        row_symbol = row.get("symbol")
        if not isinstance(row_symbol, str) or not row_symbol.strip():
            reasons.append(f"position[{index}]_symbol_missing")
            continue

        if row_symbol != symbol:
            continue

        side = row.get("side")
        if side not in ("LONG", "SHORT"):
            reasons.append(f"position[{index}]_side_invalid")
            continue

        try:
            quantity = _decimal(
                row.get("position"), f"position[{index}].position"
            )
            if quantity < 0:
                raise ValueError("position quantity negative")
        except ValueError as exc:
            reasons.append(f"position[{index}]_{exc}")
            continue

        if quantity > 0:
            try:
                avg_price = _decimal(
                    row.get("avgPrice"), f"position[{index}].avgPrice"
                )
                if avg_price <= 0:
                    raise ValueError("avgPrice must be positive")
            except ValueError as exc:
                reasons.append(f"position[{index}]_{exc}")
                continue

            active.append({
                "side": side,
                "quantity": quantity,
                "avg_price": avg_price,
            })

    if reasons:
        return _failure(*reasons)

    actual = {}
    for position in active:
        side = position["side"]
        if side in actual:
            return _failure("duplicate_active_position_side")
        actual[side] = {
            "quantity": position["quantity"],
            "avg_price": position["avg_price"],
        }

    if expected is None:
        if actual:
            return _failure("unexpected_exchange_position")

        return {
            "ok": True,
            "halt_trading": False,
            "symbol": symbol,
            "expected_position": None,
            "exchange_position": None,
            "reasons": [],
            "read_only": True,
        }

    if not actual:
        return _failure("expected_position_missing_on_exchange")
    if set(actual) != set(expected):
        return _failure("position_side_mismatch")
    for side, expected_position in expected.items():
        actual_position = actual[side]
        if abs(actual_position["quantity"] - expected_position["quantity"]) > qty_tol:
            return _failure("position_quantity_mismatch")
        price_scale = max(abs(expected_position["avg_price"]), Decimal("1"))
        if abs(actual_position["avg_price"] - expected_position["avg_price"]) > price_tol * price_scale:
            return _failure("position_average_price_mismatch")

    return {
        "ok": True,
        "halt_trading": False,
        "symbol": symbol,
        "expected_position": {
            side: {"quantity": str(position["quantity"]), "avg_price": str(position["avg_price"])}
            for side, position in expected.items()
        },
        "exchange_position": {
            side: {"quantity": str(position["quantity"]), "avg_price": str(position["avg_price"])}
            for side, position in actual.items()
        },
        "reasons": [],
        "read_only": True,
    }


def _failure(*reasons):
    return {
        "ok": False,
        "halt_trading": True,
        "symbol": None,
        "expected_position": None,
        "exchange_position": None,
        "reasons": list(reasons),
        "read_only": True,
    }
