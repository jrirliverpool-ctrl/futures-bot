"""Read-only Toobit USDT-M account preflight for ApexQuant.

This module NEVER submits, amends, or cancels orders and NEVER changes
leverage or margin mode. It only calls documented GET endpoints through
the existing ToobitClient signing/transport layer.

Run from repository root:
    python scripts/toobit_readonly_preflight.py

Required environment variables:
    TOOBIT_API_KEY
    TOOBIT_API_SECRET

A successful preflight is NOT authorization to trade.
Any open position, open order, malformed response, missing leverage
setting, incomplete order scan, or API error halts the check.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from toobit_open_orders import (
    fetch_all_open_orders,
    OpenOrdersReadError,
)


ENDPOINTS = {
    "balance": "/api/v1/futures/balance",
    "positions": "/api/v1/futures/positions",
    "open_orders": "/api/v1/futures/openOrders",
    "leverage": "/api/v1/futures/accountLeverage",
}

ROOT = Path(__file__).resolve().parents[1]
WHITELIST_PATH = ROOT / "config" / "crypto_whitelist.json"


class PreflightError(RuntimeError):
    """An error with a deliberately safe diagnostic message."""


def load_symbols(path: Path = WHITELIST_PATH) -> list[str]:
    """Load and validate the candidate symbol list from the repository."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        raise PreflightError(
            "symbol whitelist: configuration file is missing"
        ) from None
    except (OSError, json.JSONDecodeError):
        raise PreflightError(
            "symbol whitelist: configuration file is unreadable"
        ) from None

    if not isinstance(config, dict):
        raise PreflightError(
            "symbol whitelist: expected a JSON object"
        )

    symbols = config.get("symbols")

    if not isinstance(symbols, list) or not symbols:
        raise PreflightError(
            "symbol whitelist: symbols must be a non-empty array"
        )

    if not all(isinstance(item, str) for item in symbols):
        raise PreflightError(
            "symbol whitelist: every symbol must be text"
        )

    if len(symbols) != len(set(symbols)):
        raise PreflightError(
            "symbol whitelist: duplicate symbols found"
        )

    for symbol in symbols:
        if not re.fullmatch(r"[A-Z0-9]+-SWAP-USDT", symbol):
            raise PreflightError(
                "symbol whitelist: invalid USDT-M symbol format"
            )

    return symbols


def _as_list(payload: Any, label: str) -> list[dict]:
    """Accept only confirmed successful V1 list responses; fail closed."""
    if isinstance(payload, dict):
        if payload.get("_transport"):
            raise PreflightError(
                f"{label}: transport failure"
            )

        code = payload.get("code")

        if code not in (None, 0, 200, "0", "200"):
            safe_code = str(code)[:32]
            raise PreflightError(
                f"{label}: exchange error code {safe_code}"
            )

        raise PreflightError(
            f"{label}: unexpected object response"
        )

    if not isinstance(payload, list):
        raise PreflightError(
            f"{label}: expected a JSON array"
        )

    if not all(isinstance(row, dict) for row in payload):
        raise PreflightError(
            f"{label}: array contains a non-object row"
        )

    return payload


def _decimal(
    value: Any,
    label: str,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PreflightError(
            f"{label}: missing or invalid numeric value"
        )

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PreflightError(
            f"{label}: invalid numeric value"
        ) from None

    if (
        not number.is_finite()
        or (not allow_negative and number < 0)
        or (not allow_zero and number == 0)
    ):
        raise PreflightError(
            f"{label}: out-of-range numeric value"
        )

    return number


def _get_list(
    client: Any,
    path: str,
    params: dict,
    label: str,
) -> list[dict]:
    """Perform one GET and convert transport failures to safe errors."""
    try:
        payload = client._request("GET", path, params)
    except Exception:
        raise PreflightError(
            f"{label}: request failed"
        ) from None

    return _as_list(payload, label)


def _read_all_open_orders(client: Any) -> list[dict]:
    """Read the complete open-order set using descending orderId pages.

    Two consecutive scans must return the same order IDs and row contents.
    A failed, incomplete, or unstable scan blocks the preflight.
    """
    try:
        return fetch_all_open_orders(
            client,
            limit=1000,
            max_pages=100,
            verify_stable=True,
        )
    except (OpenOrdersReadError, ValueError):
        raise PreflightError(
            "futures open orders: complete stable snapshot "
            "could not be proven"
        ) from None


def _read_leverage_for_symbol(
    client: Any,
    symbol: str,
) -> dict:
    """Read and validate leverage for exactly one requested symbol."""
    rows = _get_list(
        client,
        ENDPOINTS["leverage"],
        {"symbol": symbol},
        f"futures leverage {symbol}",
    )

    matching = [
        row for row in rows
        if row.get("symbolId") == symbol
    ]

    if len(matching) != 1:
        raise PreflightError(
            f"futures leverage {symbol}: "
            "expected exactly one matching row"
        )

    row = matching[0]

    if (
        not isinstance(row.get("symbolId"), str)
        or not row["symbolId"]
    ):
        raise PreflightError(
            f"futures leverage {symbol}: missing symbolId"
        )

    _decimal(
        row.get("leverage"),
        f"futures leverage {symbol}.leverage",
        allow_zero=False,
    )

    if row.get("marginType") not in ("CROSS", "ISOLATED"):
        raise PreflightError(
            f"futures leverage {symbol}: invalid marginType"
        )

    return row


def build_snapshot(client) -> dict:
    """Read account endpoints and fail closed on any ambiguity."""
    symbols = load_symbols()

    balance_rows = _get_list(
        client,
        ENDPOINTS["balance"],
        {},
        "futures balance",
    )

    position_rows = _get_list(
        client,
        ENDPOINTS["positions"],
        {},
        "futures positions",
    )

    open_order_rows = _read_all_open_orders(client)

    # The Toobit accountLeverage endpoint requires a symbol parameter.
    # Query each configured candidate independently and verify the response.
    leverage_rows = [
        _read_leverage_for_symbol(client, symbol)
        for symbol in symbols
    ]

    usdt_rows = [
        row for row in balance_rows
        if row.get("coin") == "USDT"
    ]

    if len(usdt_rows) != 1:
        raise PreflightError(
            "futures balance: expected exactly one USDT wallet row"
        )

    usdt = usdt_rows[0]
    wallet = {}

    for key in (
        "balance",
        "availableBalance",
        "positionMargin",
        "orderMargin",
        "crossUnRealizedPnl",
        "coupon",
    ):
        if key in usdt:
            _decimal(
                usdt[key],
                f"USDT.{key}",
                allow_negative=(key == "crossUnRealizedPnl"),
            )
            wallet[key] = str(usdt[key])

    active_positions = []

    for index, row in enumerate(position_rows):
        symbol = row.get("symbol")
        side = row.get("side")

        if not isinstance(symbol, str) or not symbol.strip():
            raise PreflightError(
                f"position[{index}]: missing symbol"
            )

        if side not in ("LONG", "SHORT"):
            raise PreflightError(
                f"position[{index}]: invalid side"
            )

        quantity = _decimal(
            row.get("position"),
            f"position[{index}].position",
        )

        if quantity > 0:
            active_positions.append(row)

    for index, row in enumerate(open_order_rows):
        for field in ("orderId", "symbol", "side", "status"):
            if row.get(field) in (None, ""):
                raise PreflightError(
                    f"open_order[{index}]: missing {field}"
                )

    blockers = []

    if active_positions:
        blockers.append("open_positions_exist")

    if open_order_rows:
        blockers.append("open_orders_exist")

    return {
        "schema": "apexquant_toobit_readonly_preflight_v3",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "wallet": {
            "coin": "USDT",
            **wallet,
        },
        "positions": active_positions,
        "open_orders": open_order_rows,
        "leverage_settings": leverage_rows,
        "symbols_checked": symbols,
        "halt_live": bool(blockers),
        "blockers": blockers,
    }


def public_summary(report: dict) -> dict:
    """Return a safe-to-log summary without account values or order IDs."""
    return {
        "schema": report.get("schema"),
        "checked_at_utc": report.get("checked_at_utc"),
        "read_only": True,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "usdt_wallet_found": (
            report.get("wallet", {}).get("coin") == "USDT"
        ),
        "active_positions_count": len(
            report.get("positions", [])
        ),
        "open_orders_count": len(
            report.get("open_orders", [])
        ),
        "leverage_settings_count": len(
            report.get("leverage_settings", [])
        ),
        "symbols_checked_count": len(
            report.get("symbols_checked", [])
        ),
        "halt_live": bool(report.get("halt_live", True)),
        "blockers": list(report.get("blockers", [])),
        "account_values_redacted": True,
    }


def main() -> int:
    stage = "initialization"

    try:
        from toobit_client import ToobitClient

        stage = "client_initialization"
        client = ToobitClient()

        stage = "read_only_account_preflight"
        report = build_snapshot(client)

    except PreflightError as exc:
        print(json.dumps(
            {
                "ok": False,
                "read_only": True,
                "halt_live": True,
                "error_type": "PreflightError",
                "stage": stage,
                "diagnostic": str(exc),
                "message": (
                    "Read-only preflight failed; "
                    "do not enable LIVE."
                ),
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 2

    except Exception as exc:
        # Never print raw exception text or traceback to workflow logs.
        print(json.dumps(
            {
                "ok": False,
                "read_only": True,
                "halt_live": True,
                "error_type": type(exc).__name__,
                "stage": stage,
                "message": (
                    "Read-only preflight failed; "
                    "do not enable LIVE."
                ),
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 2

    report["ok"] = True

    print(json.dumps(
        public_summary(report),
        indent=2,
        ensure_ascii=False,
    ))

    # A successful read with open positions/orders is still a blocker.
    return 0 if not report["halt_live"] else 3


if __name__ == "__main__":
    raise SystemExit(main())