
"""Additional fail-closed readiness gate for ApexQuant Toobit execution.

READ-ONLY: never submits, amends, or cancels orders.
A passing result is necessary but NOT sufficient authorization to trade.
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(__file__))

from toobit_readonly_preflight import build_snapshot, PreflightError


def _positive_available(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("availableBalance is missing or invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("availableBalance is not numeric") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("availableBalance must be greater than zero")
    return amount


def evaluate_readiness(snapshot: dict) -> dict:
    """Evaluate a preflight snapshot without performing I/O or mutations."""
    blockers = []

    if not isinstance(snapshot, dict):
        return {
            "ok": False,
            "ready_for_manual_review": False,
            "blockers": ["invalid_preflight_snapshot"],
            "read_only": True,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
        }

    if snapshot.get("read_only") is not True:
        blockers.append("preflight_not_read_only")

    if snapshot.get("ok") is not True:
        blockers.append("base_preflight_not_successful")

    if snapshot.get("halt_live") is not False:
        blockers.append("base_preflight_halt_live")

    existing = snapshot.get("blockers")
    if not isinstance(existing, list):
        blockers.append("base_preflight_blockers_invalid")
    elif existing:
        blockers.extend(f"base:{str(item)[:96]}" for item in existing)

    wallet = snapshot.get("wallet")
    if not isinstance(wallet, dict) or wallet.get("coin") != "USDT":
        blockers.append("usdt_wallet_missing")
    else:
        try:
            _positive_available(wallet.get("availableBalance"))
        except ValueError:
            blockers.append("no_usable_available_usdt_balance")

    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        blockers.append("positions_snapshot_invalid")
    elif positions:
        blockers.append("active_positions_exist")

    orders = snapshot.get("open_orders")
    if not isinstance(orders, list):
        blockers.append("open_orders_snapshot_invalid")
    elif orders:
        blockers.append("open_orders_exist")

    symbols = snapshot.get("symbols_checked")
    leverage = snapshot.get("leverage_settings")
    if (
        not isinstance(symbols, list)
        or not symbols
        or not isinstance(leverage, list)
        or len(leverage) != len(symbols)
    ):
        blockers.append("leverage_coverage_incomplete")

    blockers = list(dict.fromkeys(blockers))

    return {
        "ok": not blockers,
        "ready_for_manual_review": not blockers,
        "blockers": blockers,
        "read_only": True,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "account_values_redacted": True,
        "manual_authorization_required": True,
    }


def main() -> int:
    stage = "initialization"

    try:
        from toobit_client import ToobitClient

        stage = "client_initialization"
        client = ToobitClient()

        stage = "read_only_account_snapshot"
        snapshot = build_snapshot(client)
        snapshot["ok"] = True

        stage = "readiness_evaluation"
        report = evaluate_readiness(snapshot)

    except PreflightError as exc:
        report = {
            "ok": False,
            "ready_for_manual_review": False,
            "blockers": ["base_preflight_failed"],
            "diagnostic": str(exc),
            "read_only": True,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
        }

    except Exception as exc:
        report = {
            "ok": False,
            "ready_for_manual_review": False,
            "blockers": ["readiness_check_failed"],
            "error_type": type(exc).__name__,
            "stage": stage,
            "read_only": True,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
        }

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") is True else 3


if __name__ == "__main__":
    raise SystemExit(main())
