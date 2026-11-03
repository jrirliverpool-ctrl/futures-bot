"""Read-only Toobit USDT-M account preflight for ApexQuant.

This module NEVER submits, amends, or cancels an order and NEVER changes
leverage or margin mode. It only calls documented GET endpoints through the
existing ToobitClient._request signing/transport layer.

Run from repository root after copying this file to scripts/:
    python scripts/toobit_readonly_preflight.py

Required environment variables:
    TOOBIT_API_KEY
    TOOBIT_API_SECRET

A successful preflight is NOT authorization to trade. Any open position,
regular/conditional order, malformed response, or API error halts the check.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(__file__))


ENDPOINTS = {
    "balance": "/api/v1/futures/balance",
    "positions": "/api/v1/futures/positions",
    "open_orders": "/api/v1/futures/openOrders",
    "leverage": "/api/v1/futures/accountLeverage",
}


def _as_list(payload, label: str) -> list[dict]:
    """Accept only confirmed successful V1 list responses; fail closed."""
    if isinstance(payload, dict):
        if payload.get("_transport"):
            raise RuntimeError(f"{label}: transport failure")
        code = payload.get("code")
        if code not in (None, 0, 200):
            raise RuntimeError(f"{label}: exchange error code {code!r}")
        # V1 endpoints documented here return top-level arrays. Do not
        # silently reinterpret arbitrary objects as an empty result.
        raise RuntimeError(f"{label}: unexpected object response")
    if not isinstance(payload, list):
        raise RuntimeError(f"{label}: expected a JSON array")
    if not all(isinstance(row, dict) for row in payload):
        raise RuntimeError(f"{label}: array contains a non-object row")
    return payload


def _decimal(value, label: str, allow_negative: bool = False, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RuntimeError(f"{label}: missing or invalid numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RuntimeError(f"{label}: invalid numeric value")
    if (
        not number.is_finite()
        or (not allow_negative and number < 0)
        or (not allow_zero and number == 0)
    ):
        raise RuntimeError(f"{label}: out-of-range numeric value")
    return number


def _get_list(client, path: str, params: dict, label: str) -> list[dict]:
    # Existing client signs GET query params and performs exactly one request.
    return _as_list(client._request("GET", path, params), label)


def build_snapshot(client) -> dict:
    """Read four independent account endpoints and fail closed on ambiguity."""
    balance_rows = _get_list(client, ENDPOINTS["balance"], {}, "futures balance")
    position_rows = _get_list(client, ENDPOINTS["positions"], {}, "futures positions")
    open_order_rows = _get_list(
        client, ENDPOINTS["open_orders"], {"limit": 1000}, "futures open orders"
    )
    leverage_rows = _get_list(client, ENDPOINTS["leverage"], {}, "futures leverage")

    usdt_rows = [row for row in balance_rows if row.get("coin") == "USDT"]
    if len(usdt_rows) != 1:
        raise RuntimeError("futures balance: expected exactly one USDT wallet row")
    usdt = usdt_rows[0]
    wallet = {}
    for key in (
        "balance", "availableBalance", "positionMargin", "orderMargin",
        "crossUnRealizedPnl", "coupon",
    ):
        if key in usdt:
            # Preserve exchange precision as strings, but validate numeric form.
            _decimal(
                usdt[key],
                f"USDT.{key}",
                allow_negative=(key == "crossUnRealizedPnl"),
            )
            wallet[key] = str(usdt[key])

    active_positions = []
    for i, row in enumerate(position_rows):
        symbol = row.get("symbol")
        side = row.get("side")
        if not isinstance(symbol, str) or not symbol.strip():
            raise RuntimeError(f"position[{i}]: missing symbol")
        if side not in ("LONG", "SHORT"):
            raise RuntimeError(f"position[{i}]: invalid side")
        qty = _decimal(row.get("position"), f"position[{i}].position")
        if qty > 0:
            active_positions.append(row)

    for i, row in enumerate(open_order_rows):
        for field in ("orderId", "symbol", "side", "status"):
            if row.get(field) in (None, ""):
                raise RuntimeError(f"open_order[{i}]: missing {field}")

    for i, row in enumerate(leverage_rows):
        if not isinstance(row.get("symbolId"), str) or not row["symbolId"]:
            raise RuntimeError(f"leverage[{i}]: missing symbolId")
        _decimal(
            row.get("leverage"),
            f"leverage[{i}].leverage",
            allow_zero=False,
        )
        if row.get("marginType") not in ("CROSS", "ISOLATED"):
            raise RuntimeError(f"leverage[{i}]: invalid marginType")

    blockers = []
    if active_positions:
        blockers.append("open_positions_exist")
    if open_order_rows:
        blockers.append("open_orders_exist")

    return {
        "schema": "apexquant_toobit_readonly_preflight_v1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "wallet": {"coin": "USDT", **wallet},
        "positions": active_positions,
        "open_orders": open_order_rows,
        "leverage_settings": leverage_rows,
        "halt_live": bool(blockers),
        "blockers": blockers,
    }


def public_summary(report: dict) -> dict:
    """Return a safe-to-log summary; never expose wallet/position details.

    The GitHub repository is public, so workflow logs must not contain
    balances, position sizes, order IDs, or raw exchange payloads.
    """
    return {
        "schema": report.get("schema"),
        "checked_at_utc": report.get("checked_at_utc"),
        "read_only": True,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "usdt_wallet_found": report.get("wallet", {}).get("coin") == "USDT",
        "active_positions_count": len(report.get("positions", [])),
        "open_orders_count": len(report.get("open_orders", [])),
        "leverage_settings_count": len(report.get("leverage_settings", [])),
        "halt_live": bool(report.get("halt_live", True)),
        "blockers": list(report.get("blockers", [])),
        "account_values_redacted": True,
    }


def main() -> int:
    try:
        from toobit_client import ToobitClient
        client = ToobitClient()
        report = build_snapshot(client)
    except Exception as exc:  # no raw exception; may contain request details
        print(json.dumps({
            "ok": False,
            "read_only": True,
            "halt_live": True,
            "error_type": type(exc).__name__,
            "message": "Read-only account preflight failed; do not enable LIVE.",
        }, indent=2, ensure_ascii=False))
        return 2

    report["ok"] = True
    print(json.dumps(public_summary(report), indent=2, ensure_ascii=False))
    return 0 if not report["halt_live"] else 3


if __name__ == "__main__":
    raise SystemExit(main())