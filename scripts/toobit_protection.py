
"""Fail-closed Toobit V1 position-protection helper.

This module is deliberately NOT wired to the trading runner.
It is intended for unit-tested integration after an independent
live-execution review.

It sends one signed POST to set fixed position SL/TP, then verifies
the exchange's open STOP_PROFIT_LOSS orders using a read-only GET.

It never retries a POST and treats every ambiguous result as unsafe.
"""
from decimal import Decimal, InvalidOperation
import re

ENDPOINT = "/api/v1/futures/position/trading-stop"
OPEN_ORDERS_ENDPOINT = "/api/v1/futures/openOrders"


class ProtectionError(RuntimeError):
    """Protection could not be proven active at the exchange."""


def _positive_decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a positive decimal")

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(
            f"{field} must be a positive decimal"
        ) from None

    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field} must be finite and > 0")

    return number


def _same_decimal(left, right):
    try:
        return (
            _positive_decimal(left, "exchange value")
            == _positive_decimal(right, "expected value")
        )
    except ValueError:
        return False


def _validate_payload(
    payload,
    expected_symbol,
    expected_side,
    stop_loss,
    take_profit,
):
    if not isinstance(payload, dict):
        raise ProtectionError(
            "trading-stop POST returned a non-object response"
        )

    if payload.get("_transport"):
        raise ProtectionError(
            "trading-stop POST transport outcome is unknown"
        )

    http_status = payload.get("_http_status")

    if http_status is not None and not 200 <= http_status < 300:
        raise ProtectionError(
            "trading-stop POST returned HTTP error"
        )

    code = payload.get("code")

    if code is not None and code not in (0, 200, "0", "200"):
        raise ProtectionError(
            "trading-stop POST returned exchange error"
        )

    if (
        payload.get("symbol") != expected_symbol
        or payload.get("side") != expected_side
    ):
        raise ProtectionError(
            "trading-stop POST echo does not match requested position"
        )

    if not _same_decimal(payload.get("stopLoss"), stop_loss):
        raise ProtectionError(
            "trading-stop POST did not echo the requested stop loss"
        )

    if take_profit is not None:
        if not _same_decimal(
            payload.get("takeProfit"),
            take_profit,
        ):
            raise ProtectionError(
                "trading-stop POST did not echo the requested take profit"
            )


def _verify_open_orders(
    rows,
    symbol,
    side,
    quantity,
    stop_loss,
    take_profit,
):
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise ProtectionError(
            "openOrders verification returned an invalid response"
        )

    if side == "LONG":
        loss_type = "STOP_LONG_LOSS"
        profit_type = "STOP_LONG_PROFIT"
        close_side = "SELL_CLOSE"
    else:
        loss_type = "STOP_SHORT_LOSS"
        profit_type = "STOP_SHORT_PROFIT"
        close_side = "BUY_CLOSE"

    relevant = [
        row
        for row in rows
        if (
            row.get("symbol") == symbol
            and row.get("type") == "STOP_PROFIT_LOSS"
            and row.get("orderType") in {
                loss_type,
                profit_type,
            }
        )
    ]

    def matches(order_type, price):
        return [
            row
            for row in relevant
            if (
                row.get("orderType") == order_type
                and row.get("side") == close_side
                and row.get("status") == "ORDER_NEW"
                and _same_decimal(row.get("stopPrice"), price)
                and _same_decimal(row.get("origQty"), quantity)
            )
        ]

    if len(matches(loss_type, stop_loss)) != 1:
        raise ProtectionError(
            "exchange stop-loss order could not be uniquely verified"
        )

    if take_profit is not None:
        if len(matches(profit_type, take_profit)) != 1:
            raise ProtectionError(
                "exchange take-profit order could not be uniquely verified"
            )

    return {
        "symbol": symbol,
        "side": side,
        "quantity": str(quantity),
        "stop_loss_verified": True,
        "take_profit_verified": take_profit is not None,
        "orders_submitted": 0,
        "orders_canceled": 0,
        "verified_open_protection_orders": (
            1 + (1 if take_profit is not None else 0)
        ),
        "halt_trading": False,
    }


def set_and_verify_fixed_protection(
    client,
    *,
    symbol,
    side,
    quantity,
    stop_loss,
    take_profit=None,
    trigger_by="MARK_PRICE",
):
    """Set fixed SL (required) and optional TP; verify exact live orders.

    The POST is issued exactly once. A failed/ambiguous POST is never retried.

    halt_trading=False means the request echo and exchange-side conditional
    order(s) were verified. It does NOT authorize the strategy's LIVE runner.
    """
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

    qty = _positive_decimal(quantity, "quantity")
    sl = _positive_decimal(stop_loss, "stop_loss")

    tp = (
        _positive_decimal(take_profit, "take_profit")
        if take_profit is not None
        else None
    )

    if tp is not None:
        if (
            side == "LONG" and sl >= tp
        ) or (
            side == "SHORT" and sl <= tp
        ):
            raise ValueError(
                "stop_loss and take_profit are on invalid sides of each other"
            )

    if trigger_by not in ("MARK_PRICE", "CONTRACT_PRICE"):
        raise ValueError(
            "trigger_by must be MARK_PRICE or CONTRACT_PRICE"
        )

    params = {
        "symbol": symbol,
        "side": side,
        "stopLoss": str(sl),
        "slSize": str(qty),
        "slTriggerBy": trigger_by,
    }

    if tp is not None:
        params.update({
            "takeProfit": str(tp),
            "tpSize": str(qty),
            "tpTriggerBy": trigger_by,
        })

    try:
        post_result = client._request(
            "POST",
            ENDPOINT,
            params,
        )
    except Exception:
        raise ProtectionError(
            "trading-stop POST raised; outcome is unknown; do not retry"
        ) from None

    _validate_payload(
        post_result,
        symbol,
        side,
        sl,
        tp,
    )

    try:
        rows = client._request(
            "GET",
            OPEN_ORDERS_ENDPOINT,
            {
                "symbol": symbol,
                "type": "STOP_PROFIT_LOSS",
                "limit": 1000,
            },
        )
    except Exception:
        raise ProtectionError(
            "protection POST may have succeeded but verification GET failed; halt trading"
        ) from None

    if isinstance(rows, dict) and rows.get("_transport"):
        raise ProtectionError(
            "protection POST may have succeeded but verification GET is unknown; halt trading"
        )

    return _verify_open_orders(
        rows,
        symbol,
        side,
        qty,
        sl,
        tp,
    )
