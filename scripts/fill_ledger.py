
"""Fail-closed append-only Toobit futures fill ledger.

Supports V1 and V2 user-trades rows.
No order placement, cancellation, balance mutation or portfolio mutation.
Single writer process per ledger path is required.
"""

import json
import os
from decimal import Decimal, InvalidOperation


VALID_ORDER_STATUSES = {
    "PENDING_NEW",
    "NEW",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
    "FILLED",
    "CANCELED",
    "REJECTED",
}

VALID_TRADE_SIDES = {
    "BUY_OPEN",
    "SELL_OPEN",
    "BUY_CLOSE",
    "SELL_CLOSE",
}

V2_SIDE_MAP = {
    ("BUY", "LONG"): "BUY_OPEN",
    ("SELL", "SHORT"): "SELL_OPEN",
    ("SELL", "LONG"): "SELL_CLOSE",
    ("BUY", "SHORT"): "BUY_CLOSE",
}


def _decimal(value, field, *, allow_zero=True, signed=False):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be numeric")

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be numeric") from None

    if not number.is_finite():
        raise ValueError(f"{field} must be finite")

    if not signed and number < 0:
        raise ValueError(f"{field} must be >= 0")

    if not allow_zero and number == 0:
        raise ValueError(f"{field} must be > 0")

    return number


def _canonical_trade_side(raw):
    side = raw.get("side")
    position_side = raw.get("positionSide")

    # V1 rows already contain a combined side.
    if side in VALID_TRADE_SIDES:
        if position_side not in (None, ""):
            raise ValueError(
                "legacy trade side must not include positionSide"
            )
        return side, None, None

    # V2 rows use independent side and positionSide fields.
    if side in ("BUY", "SELL") and position_side in ("LONG", "SHORT"):
        canonical = V2_SIDE_MAP.get((side, position_side))

        if canonical is None:
            raise ValueError(
                "unsupported V2 side/positionSide combination"
            )

        return canonical, side, position_side

    raise ValueError(
        f"invalid trade side fields: {side!r}/{position_side!r}"
    )


def normalize_user_trade(
    raw,
    expected_symbol=None,
    expected_order_id=None,
):
    """Validate and normalize one V1 or V2 user-trades record."""

    if not isinstance(raw, dict):
        raise ValueError("trade row must be a dictionary")

    trade_id = raw.get("id")
    order_id = raw.get("orderId")
    symbol = raw.get("symbol")
    commission_asset = raw.get("commissionAsset")
    timestamp = raw.get("time")

    if trade_id is None or not str(trade_id).strip():
        raise ValueError("trade id missing")

    if order_id is None or not str(order_id).strip():
        raise ValueError("orderId missing")

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol missing")

    if (
        not isinstance(commission_asset, str)
        or not commission_asset.strip()
    ):
        raise ValueError("commissionAsset missing")

    canonical_side, exchange_side, position_side = (
        _canonical_trade_side(raw)
    )

    if expected_symbol is not None and symbol != expected_symbol:
        raise ValueError(
            "trade symbol does not match expected symbol"
        )

    if (
        expected_order_id is not None
        and str(order_id) != str(expected_order_id)
    ):
        raise ValueError(
            "trade orderId does not match expected orderId"
        )

    if isinstance(timestamp, bool) or timestamp is None:
        raise ValueError("trade time missing or invalid")

    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "trade time must be an integer timestamp"
        ) from None

    if timestamp_int < 0 or str(timestamp).strip() not in (
        str(timestamp_int),
        f"+{timestamp_int}",
    ):
        raise ValueError(
            "trade time must be a non-negative integer timestamp"
        )

    qty = _decimal(
        raw.get("qty"),
        "qty",
        allow_zero=False,
    )

    price = _decimal(
        raw.get("price"),
        "price",
        allow_zero=False,
    )

    commission = _decimal(
        raw.get("commission"),
        "commission",
    )

    maker_rebate = _decimal(
        raw.get("makerRebate", "0"),
        "makerRebate",
    )

    realized_pnl = _decimal(
        raw.get("realizedPnl", "0"),
        "realizedPnl",
        signed=True,
    )

    is_maker = raw.get("isMaker")

    if is_maker is not None and not isinstance(is_maker, bool):
        raise ValueError(
            "isMaker must be boolean when present"
        )

    return {
        "trade_id": str(trade_id),
        "order_id": str(order_id),
        "symbol": symbol,
        "timestamp_ms": timestamp_int,
        "side": canonical_side,
        "exchange_side": exchange_side,
        "position_side": position_side,
        "qty": str(qty),
        "price": str(price),
        "commission": str(commission),
        "commission_asset": commission_asset,
        "maker_rebate": str(maker_rebate),
        "realized_pnl": str(realized_pnl),
        "is_maker": is_maker,
        "ticket_id": (
            str(raw["ticketId"])
            if raw.get("ticketId") is not None
            else None
        ),
        "trade_type": (
            str(raw["type"])
            if raw.get("type") is not None
            else None
        ),
    }


def _stored_row_to_raw(row):
    """Reconstruct a normalizable row from a ledger JSON object."""

    side = row.get("side")
    exchange_side = row.get("exchange_side")
    position_side = row.get("position_side")

    if exchange_side is not None or position_side is not None:
        side_value = exchange_side
        position_value = position_side
    else:
        side_value = side
        position_value = None

    return {
        "id": row.get("trade_id"),
        "orderId": row.get("order_id"),
        "symbol": row.get("symbol"),
        "time": row.get("timestamp_ms"),
        "side": side_value,
        "positionSide": position_value,
        "qty": row.get("qty"),
        "price": row.get("price"),
        "commission": row.get("commission"),
        "commissionAsset": row.get("commission_asset"),
        "makerRebate": row.get("maker_rebate", "0"),
        "realizedPnl": row.get("realized_pnl", "0"),
        "isMaker": row.get("is_maker"),
        "ticketId": row.get("ticket_id"),
        "type": row.get("trade_type"),
    }


def _identity(row):
    """Composite exchange trade identity."""
    return (
        row["trade_id"],
        row["symbol"],
        row["order_id"],
    )


def load_fill_ledger(path):
    """Load JSONL ledger; malformed rows or duplicates fail closed."""

    if not os.path.exists(path):
        return []

    rows = []
    seen_identity = {}
    seen_trade_id = {}

    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue

            try:
                stored = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSONL at line {line_number}: {exc}"
                ) from None

            normalized = normalize_user_trade(
                _stored_row_to_raw(stored)
            )

            identity = _identity(normalized)
            trade_id = normalized["trade_id"]

            if (
                trade_id in seen_trade_id
                and seen_trade_id[trade_id] != identity
            ):
                raise ValueError(
                    "trade_id identity conflict at line "
                    f"{line_number}: {trade_id}"
                )

            if identity in seen_identity:
                if seen_identity[identity] != normalized:
                    raise ValueError(
                        "conflicting duplicate trade at line "
                        f"{line_number}: {identity}"
                    )

                raise ValueError(
                    "duplicate trade_id in ledger at line "
                    f"{line_number}: {trade_id}"
                )

            seen_trade_id[trade_id] = identity
            seen_identity[identity] = normalized
            rows.append(normalized)

    return rows


def append_user_trades(path, raw_trades):
    """Append unseen trades; exact repeats skip; conflicting identities fail."""

    if not isinstance(raw_trades, list):
        raise ValueError("raw_trades must be a list")

    existing = load_fill_ledger(path)

    known_identity = {
        _identity(row): row
        for row in existing
    }

    known_trade_ids = {
        row["trade_id"]: _identity(row)
        for row in existing
    }

    batch_identity = {}
    batch_trade_ids = {}
    to_append = []
    skipped = 0

    for raw in raw_trades:
        row = normalize_user_trade(raw)
        identity = _identity(row)
        trade_id = row["trade_id"]

        if (
            trade_id in known_trade_ids
            and known_trade_ids[trade_id] != identity
        ):
            raise ValueError(
                f"conflicting existing trade_id identity: {trade_id}"
            )

        if (
            trade_id in batch_trade_ids
            and batch_trade_ids[trade_id] != identity
        ):
            raise ValueError(
                f"conflicting batch trade_id identity: {trade_id}"
            )

        if identity in known_identity:
            if known_identity[identity] != row:
                raise ValueError(
                    f"conflicting existing trade: {identity}"
                )

            skipped += 1
            continue

        if identity in batch_identity:
            if batch_identity[identity] != row:
                raise ValueError(
                    f"conflicting duplicate trade in batch: {identity}"
                )

            skipped += 1
            continue

        batch_identity[identity] = row
        batch_trade_ids[trade_id] = identity
        to_append.append(row)

    if to_append:
        os.makedirs(
            os.path.dirname(path) or ".",
            exist_ok=True,
        )

        with open(path, "a", encoding="utf-8") as handle:
            for row in to_append:
                handle.write(
                    json.dumps(
                        row,
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n"
                )

            handle.flush()
            os.fsync(handle.fileno())

    return len(to_append), skipped


def _canonical_order_side(order):
    side = order.get("side")
    position_side = order.get("positionSide")

    if side in VALID_TRADE_SIDES:
        if position_side not in (None, ""):
            raise ValueError(
                "legacy order side unexpectedly has positionSide"
            )

        return side

    if side in ("BUY", "SELL") and position_side in ("LONG", "SHORT"):
        canonical = V2_SIDE_MAP.get((side, position_side))

        if canonical is not None:
            return canonical

    raise ValueError(
        "invalid order side/positionSide"
    )


def reconcile_order_fills(
    order,
    fills,
    tolerance="0.00000001",
):
    """Compare one order snapshot with its complete trade-level fills."""

    reasons = []

    if not isinstance(order, dict):
        return {
            "ok": False,
            "halt_trading": True,
            "reasons": ["order snapshot is not a dict"],
        }

    if not isinstance(fills, list):
        return {
            "ok": False,
            "halt_trading": True,
            "reasons": ["fills is not a list"],
        }

    order_id = order.get("orderId")
    symbol = order.get("symbol")
    status = order.get("status")

    if order_id is None or not str(order_id).strip():
        reasons.append("orderId missing")

    if not isinstance(symbol, str) or not symbol.strip():
        reasons.append("symbol missing")

    if status not in VALID_ORDER_STATUSES:
        reasons.append(
            f"unknown order status: {status!r}"
        )

    try:
        expected_side = _canonical_order_side(order)
    except ValueError as exc:
        expected_side = None
        reasons.append(str(exc))

    try:
        original_qty = _decimal(
            order.get("origQty"),
            "origQty",
            allow_zero=False,
        )

        reported_filled = _decimal(
            order.get("executedQty"),
            "executedQty",
        )

        avg_price = _decimal(
            order.get("avgPrice", "0"),
            "avgPrice",
        )

        tol = _decimal(
            tolerance,
            "tolerance",
        )

    except ValueError as exc:
        reasons.append(str(exc))
        original_qty = Decimal("0")
        reported_filled = Decimal("0")
        avg_price = Decimal("0")
        tol = Decimal("0")

    seen_identity = set()
    seen_trade_ids = {}

    total_qty = Decimal("0")
    total_value = Decimal("0")

    commissions = {}
    maker_rebates = {}
    realized_pnl_total = Decimal("0")

    for index, raw in enumerate(fills):
        try:
            fill = normalize_user_trade(
                raw,
                expected_symbol=symbol,
                expected_order_id=order_id,
            )
        except ValueError as exc:
            reasons.append(
                f"fill[{index}]: {exc}"
            )
            continue

        identity = _identity(fill)
        trade_id = fill["trade_id"]

        if (
            expected_side is not None
            and fill["side"] != expected_side
        ):
            reasons.append(
                f"fill[{index}]: trade side does not match order side"
            )
            continue

        # Detect duplicate trade IDs explicitly, including exact repeats.
        if trade_id in seen_trade_ids:
            if seen_trade_ids[trade_id] != identity:
                reasons.append(
                    "conflicting trade_id identity in fills: "
                    f"{trade_id}"
                )
            else:
                reasons.append(
                    f"duplicate trade_id in fills: {trade_id}"
                )

            continue

        if identity in seen_identity:
            reasons.append(
                f"duplicate trade identity in fills: {identity}"
            )
            continue

        seen_trade_ids[trade_id] = identity
        seen_identity.add(identity)

        qty = Decimal(fill["qty"])
        price = Decimal(fill["price"])

        total_qty += qty
        total_value += qty * price

        asset = fill["commission_asset"]

        commissions[asset] = (
            commissions.get(asset, Decimal("0"))
            + Decimal(fill["commission"])
        )

        maker_rebates[asset] = (
            maker_rebates.get(asset, Decimal("0"))
            + Decimal(fill["maker_rebate"])
        )

        realized_pnl_total += Decimal(
            fill["realized_pnl"]
        )

    if total_qty - original_qty > tol:
        reasons.append(
            "ledger filled quantity exceeds original quantity"
        )

    if abs(total_qty - reported_filled) > tol:
        reasons.append(
            "ledger filled quantity does not match order executedQty"
        )

    weighted_avg = (
        total_value / total_qty
        if total_qty > 0
        else Decimal("0")
    )

    if reported_filled > 0 and abs(
        weighted_avg - avg_price
    ) > max(
        tol,
        abs(avg_price) * Decimal("0.000001"),
    ):
        reasons.append(
            "ledger weighted average price does not match order avgPrice"
        )

    if reported_filled <= tol and avg_price > tol:
        reasons.append(
            "zero executedQty conflicts with non-zero avgPrice"
        )

    if status == "FILLED" and abs(
        reported_filled - original_qty
    ) > tol:
        reasons.append(
            "FILLED status but executedQty is not origQty"
        )

    if (
        status in {"NEW", "PENDING_NEW", "REJECTED"}
        and reported_filled > tol
    ):
        reasons.append(
            f"{status} status conflicts with non-zero executedQty"
        )

    if status == "PARTIALLY_FILLED" and not (
        tol < reported_filled < original_qty - tol
    ):
        reasons.append(
            "PARTIALLY_FILLED status has invalid executedQty"
        )

    return {
        "ok": not reasons,
        "halt_trading": (
            bool(reasons)
            or status not in VALID_ORDER_STATUSES
        ),
        "reasons": reasons,
        "order_id": (
            str(order_id)
            if order_id is not None
            else None
        ),
        "symbol": symbol,
        "status": status,
        "original_qty": float(original_qty),
        "reported_filled_qty": float(reported_filled),
        "ledger_filled_qty": float(total_qty),
        "weighted_avg_price": float(weighted_avg),
        "commissions": {
            asset: float(value)
            for asset, value in commissions.items()
        },
        "maker_rebates": {
            asset: str(value)
            for asset, value in maker_rebates.items()
        },
        "realized_pnl": str(realized_pnl_total),
        "fill_count": len(seen_identity),
    }
