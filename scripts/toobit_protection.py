"""Fail-closed Toobit V1 fixed-position protection helper.

This helper is deliberately not connected to the trading runner.
It sends one trading-stop POST, then verifies the resulting conditional
orders through a complete, stable, read-only openOrders scan.

It never retries a POST.
"""
from decimal import Decimal, InvalidOperation
import re

from toobit_open_orders import (
    fetch_all_open_orders,
    OpenOrdersReadError,
)

ENDPOINT = "/api/v1/futures/position/trading-stop"
SUCCESS_CODES = (0, 200, "0", "200")


class ProtectionError(RuntimeError):
    """Exchange-side protection could not be proven active."""


def _positive(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a positive decimal")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(
            f"{field} must be a positive decimal"
        ) from None

    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be finite and > 0")

    return result


def _same(left, right):
    try:
        return (
            _positive(left, "exchange value")
            == _positive(right, "expected value")
        )
    except ValueError:
        return False


def _validate_post_result(payload):
    if not isinstance(payload, dict):
        raise ProtectionError(
            "trading-stop POST returned a non-object response"
        )

    if payload.get("_transport"):
        raise ProtectionError(
            "trading-stop POST outcome is unknown; do not retry"
        )

    status = payload.get("_http_status")

    if status is not None:
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 200 <= status < 300
        ):
            raise ProtectionError(
                "trading-stop POST returned HTTP error"
            )

    code = payload.get("code")

    if code is not None and code not in SUCCESS_CODES:
        raise ProtectionError(
            "trading-stop POST returned exchange error"
        )


def _verify_orders(
    rows,
    symbol,
    side,
    stop_qty,
    stop_price,
    tp_qty,
    tp_price,
):
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise ProtectionError(
            "openOrders verification returned invalid response"
        )

    if side == "LONG":
        sl_type = "STOP_LONG_LOSS"
        tp_type = "STOP_LONG_PROFIT"
        close_side = "SELL_CLOSE"
    else:
        sl_type = "STOP_SHORT_LOSS"
        tp_type = "STOP_SHORT_PROFIT"
        close_side = "BUY_CLOSE"

    relevant = [
        row
        for row in rows
        if (
            row.get("symbol") == symbol
            and row.get("type") == "STOP_PROFIT_LOSS"
        )
    ]

    def exact(order_type, quantity, price):
        return [
            row
            for row in relevant
            if (
                row.get("orderType") == order_type
                and row.get("side") == close_side
                and row.get("status") == "ORDER_NEW"
                and _same(row.get("stopPrice"), price)
                and _same(row.get("origQty"), quantity)
            )
        ]

    sl_matches = exact(sl_type, stop_qty, stop_price)

    if len(sl_matches) != 1:
        raise ProtectionError(
            "exchange stop-loss could not be uniquely verified"
        )

    tp_verified = False

    if tp_qty is not None:
        tp_matches = exact(tp_type, tp_qty, tp_price)

        if len(tp_matches) != 1:
            raise ProtectionError(
                "exchange partial take-profit could not be uniquely verified"
            )

        tp_verified = True

    return {
        "symbol": symbol,
        "side": side,
        "stop_loss_quantity": str(stop_qty),
        "take_profit_quantity": (
            str(tp_qty) if tp_qty is not None else None
        ),
        "stop_loss_verified": True,
        "take_profit_verified": tp_verified,
        "verified_open_protection_orders": 1 + int(tp_verified),
        "orders_submitted": 0,
        "orders_canceled": 0,
        "halt_trading": False,
        "verification_scans": 2,
    }


def set_and_verify_fixed_protection(
    client,
    *,
    symbol,
    side,
    quantity,
    stop_loss,
    take_profit=None,
    take_profit_quantity=None,
    trigger_by="MARK_PRICE",
):
    """Set full-size fixed SL and optional explicitly sized partial TP."""
    if client is None or not callable(
        getattr(client, "_request", None)
    ):
        raise ValueError("client must provide _request")

    if (
        not isinstance(symbol, str)
        or not re.fullmatch(r"[A-Z0-9]+-SWAP-USDT", symbol)
    ):
        raise ValueError(
            "symbol must be a valid USDT-M swap symbol"
        )

    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")

    if trigger_by not in ("MARK_PRICE", "CONTRACT_PRICE"):
        raise ValueError("invalid trigger_by")

    qty = _positive(quantity, "quantity")
    sl = _positive(stop_loss, "stop_loss")

    tp = (
        _positive(take_profit, "take_profit")
        if take_profit is not None
        else None
    )

    tp_qty = (
        _positive(
            take_profit_quantity,
            "take_profit_quantity",
        )
        if take_profit_quantity is not None
        else None
    )

    if (tp is None) != (tp_qty is None):
        raise ValueError(
            "take_profit and take_profit_quantity must be supplied together"
        )

    if tp_qty is not None and tp_qty > qty:
        raise ValueError(
            "take_profit_quantity cannot exceed position quantity"
        )

    if tp is not None:
        if (
            (side == "LONG" and sl >= tp)
            or (side == "SHORT" and sl <= tp)
        ):
            raise ValueError(
                "stop_loss and take_profit are on invalid sides"
            )

    params = {
        "symbol": symbol,
        "side": side,
        "stopLoss": str(sl),
        "slSize": str(qty),
        "slTriggerBy": trigger_by,
        "stopType": "FIXED_STOP",
    }

    if tp is not None:
        params.update({
            "takeProfit": str(tp),
            "tpSize": str(tp_qty),
            "tpTriggerBy": trigger_by,
        })

    # Exactly one POST. Never retry an ambiguous submission.
    try:
        post_result = client._request(
            "POST",
            ENDPOINT,
            params,
        )
    except Exception:
        raise ProtectionError(
            "trading-stop POST raised; outcome unknown; do not retry"
        ) from None

    _validate_post_result(post_result)

    try:
        rows = fetch_all_open_orders(
            client,
            symbol=symbol,
            order_type="STOP_PROFIT_LOSS",
            limit=1000,
            max_pages=100,
            verify_stable=True,
        )
    except (OpenOrdersReadError, ValueError):
        raise ProtectionError(
            "complete stable protection-order snapshot could not be proven; halt"
        ) from None

    return _verify_orders(
        rows,
        symbol,
        side,
        qty,
        sl,
        tp_qty,
        tp,
    )