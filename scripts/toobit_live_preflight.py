
"""ApexQuant local Toobit preflight.

READ-ONLY ONLY:
- Reads Toobit positions twice and requires a stable snapshot.
- Reads V2 conditional orders twice and requires a stable snapshot.
- Reads official symbol specifications.
- Checks the durable order-intent journal.
- Never submits, amends, or cancels an exchange order.
- Never changes leverage or margin mode.

This is a preflight diagnostic, NOT a live trading authorization.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from toobit_client import ToobitClient
from order_intent_store import OrderIntentStore
from symbol_specs import get_symbol_specs, validate_specs
from toobit_position_snapshot import (
    fetch_stable_position_snapshot,
    PositionSnapshotError,
)
from toobit_v2_algo_snapshot import (
    fetch_stable_v2_algo_order_snapshot,
    AlgoOrderSnapshotError,
)


DEFAULT_SYMBOL = "BTC-SWAP-USDT"
DEFAULT_JOURNAL = PROJECT_ROOT / "runtime" / "order_intents.jsonl"


def emit(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def active_positions(rows: list) -> list:
    result = []

    for row in rows:
        try:
            qty = float(row.get("position"))
        except (TypeError, ValueError):
            raise ValueError("position_quantity_invalid")

        if qty < 0:
            raise ValueError("negative_position_quantity")

        if qty > 0:
            result.append({
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "quantity": str(row.get("position")),
                "entry_price": str(row.get("avgPrice")),
                "leverage": row.get("leverage"),
                "margin_type": row.get("marginType"),
            })

    return result


def main() -> int:
    symbol = os.environ.get(
        "APEXQUANT_SYMBOL",
        DEFAULT_SYMBOL,
    ).strip().upper()

    journal_path = Path(
        os.environ.get(
            "APEXQUANT_INTENT_JOURNAL",
            str(DEFAULT_JOURNAL),
        )
    ).expanduser().resolve()

    report = {
        "program": "ApexQuant Toobit Local Preflight",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY",
        "symbol": symbol,
        "checks": {},
        "live_order_authorized": False,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
    }

    # ---------------------------------------------------------
    # 1. Credentials and client initialization
    # ---------------------------------------------------------

    try:
        client = ToobitClient()
        report["checks"]["credentials"] = {
            "ok": True,
            "message": "API credentials loaded from environment",
        }
    except Exception as exc:
        report["checks"]["credentials"] = {
            "ok": False,
            "reason": type(exc).__name__,
        }
        report["result"] = "BLOCKED"
        emit(report)
        return 1

    # ---------------------------------------------------------
    # 2. Official symbol specifications
    # ---------------------------------------------------------

    try:
        specs = get_symbol_specs(symbol)
        errors, warnings = validate_specs(specs)

        report["checks"]["symbol_specs"] = {
            "ok": len(errors) == 0,
            "symbol": specs.get("symbol"),
            "status": specs.get("status"),
            "quantity_unit": specs.get("quantity_unit"),
            "contract_multiplier": specs.get("contract_multiplier"),
            "min_contract_qty": specs.get("min_contract_qty"),
            "contract_qty_step": specs.get("contract_qty_step"),
            "min_notional": specs.get("min_notional"),
            "min_notional_source": specs.get("min_notional_source"),
            "min_notional_verified": specs.get("min_notional_verified"),
            "max_leverage": specs.get("max_leverage"),
            "errors": errors,
            "warnings": warnings,
        }
    except Exception as exc:
        report["checks"]["symbol_specs"] = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    # ---------------------------------------------------------
    # 3. Stable exchange position snapshot
    # ---------------------------------------------------------

    positions = None

    try:
        positions = fetch_stable_position_snapshot(client)
        active = active_positions(positions)

        report["checks"]["positions"] = {
            "ok": True,
            "stable_reads": 2,
            "active_position_count": len(active),
            "active_positions": active,
            "first_entry_condition": len(active) == 0,
        }
    except Exception as exc:
        report["checks"]["positions"] = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    # ---------------------------------------------------------
    # 4. Stable V2 conditional-order snapshot
    # ---------------------------------------------------------

    try:
        snapshot = fetch_stable_v2_algo_order_snapshot(
            client,
            symbol=None,
            limit=1000,
        )

        rows = snapshot.get("orders", [])

        report["checks"]["conditional_orders"] = {
            "ok": (
                snapshot.get("stable") is True
                and snapshot.get("scans") == 2
                and snapshot.get("read_only") is True
            ),
            "stable_reads": snapshot.get("scans"),
            "categories_checked": snapshot.get("categories_checked"),
            "active_conditional_order_count": len(rows),
            "orders": [
                {
                    "symbol": row.get("symbol"),
                    "order_id": row.get("orderId"),
                    "type": row.get("type"),
                    "stop_order_type": row.get("stopOrderType"),
                    "position_side": row.get("positionSide"),
                    "side": row.get("side"),
                    "quantity": row.get("origQty"),
                    "stop_price": row.get("stopPrice"),
                    "status": row.get("status"),
                }
                for row in rows
            ],
        }
    except Exception as exc:
        report["checks"]["conditional_orders"] = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    # ---------------------------------------------------------
    # 5. Durable order-intent journal
    # ---------------------------------------------------------

    try:
        store = OrderIntentStore(str(journal_path))
        unresolved = store.recovery_required()

        report["checks"]["intent_journal"] = {
            "ok": len(unresolved) == 0,
            "path": str(journal_path),
            "unresolved_count": len(unresolved),
            "unresolved": [
                {
                    "client_order_id": item.get("client_order_id"),
                    "status": item.get("status"),
                    "symbol": (item.get("intent") or {}).get("symbol"),
                }
                for item in unresolved
            ],
        }
    except Exception as exc:
        report["checks"]["intent_journal"] = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    # ---------------------------------------------------------
    # 6. Final result
    # ---------------------------------------------------------

    all_checks_ok = all(
        item.get("ok") is True
        for item in report["checks"].values()
    )

    no_active_positions = (
        report["checks"].get("positions", {}).get(
            "active_position_count"
        ) == 0
    )

    report["result"] = (
        "READ_ONLY_PREFLIGHT_PASSED"
        if all_checks_ok and no_active_positions
        else "BLOCKED"
    )

    report["live_order_authorized"] = False
    report["important"] = (
        "Passing this diagnostic does not authorize live trading. "
        "Regular open orders, available margin, account mode, "
        "entry submission, fill reconciliation and V2 stop placement "
        "must be independently verified before a live order."
    )

    emit(report)

    return 0 if report["result"] == "READ_ONLY_PREFLIGHT_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
