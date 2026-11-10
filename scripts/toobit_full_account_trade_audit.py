
"""Read-only Toobit USDT-M full-account trade and balance-flow audit.

Fetches complete V2 trade history for configured symbols, stable account
positions/open orders, and the exchange's queryable futures balance-flow
history. This module never submits, amends, or cancels orders.
It never enables live execution.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fill_ledger import append_user_trades, load_fill_ledger
from toobit_client import ToobitClient
from toobit_trade_pagination import (
    TradePaginationError,
    fetch_complete_user_trades_v2,
)
from toobit_open_orders import (
    OpenOrdersReadError,
    fetch_all_open_orders,
)
from toobit_open_algo_orders import (
    OpenAlgoOrdersReadError,
    fetch_all_open_algo_orders,
)
from toobit_balance_flow import (
    BalanceFlowError,
    fetch_all_futures_balance_flow,
    summarize_balance_flow,
)
from toobit_position_snapshot import (
    POSITIONS_ENDPOINT,
    PositionSnapshotError,
    fetch_stable_position_snapshot,
)
from toobit_commission_rates import (
    CommissionRateError,
    fetch_stable_commission_rates,
)

BALANCE_FLOW_SCOPE = "exchange_queryable_recent_90_days"


class AuditError(RuntimeError):
    pass


def load_symbols(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuditError(
            f"whitelist_read_failed:{type(exc).__name__}"
        ) from None

    symbols = payload.get("symbols") if isinstance(payload, dict) else None

    if not isinstance(symbols, list) or not symbols:
        raise AuditError("whitelist_symbols_missing")

    if any(not isinstance(s, str) or not s.strip() for s in symbols):
        raise AuditError("whitelist_symbol_invalid")

    if len(symbols) != len(set(symbols)):
        raise AuditError("whitelist_duplicate_symbol")

    return symbols


def _decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise AuditError(f"{field}_missing_or_invalid")

    try:
        number = Decimal(str(value))
    except Exception:
        raise AuditError(f"{field}_not_numeric") from None

    if not number.is_finite():
        raise AuditError(f"{field}_not_finite")

    return number


def _position_unrealized_pnl(row):
    """Prefer Toobit's documented V1 field, with legacy fallback."""
    value = row.get("unrealizedPnL")

    if value is None:
        value = row.get("unRealizedProfit", "")

    if value is None:
        return ""

    return str(value)


def _read_positions(client, rows=None) -> list[dict]:
    if rows is None:
        try:
            rows = fetch_stable_position_snapshot(client)
        except PositionSnapshotError as exc:
            raise AuditError(
                f"positions_snapshot_not_stable:{str(exc)[:120]}"
            ) from None

    if isinstance(rows, dict) and rows.get("_transport"):
        raise AuditError("positions_transport_unknown")

    if not isinstance(rows, list):
        raise AuditError("positions_snapshot_not_list")

    if not all(isinstance(row, dict) for row in rows):
        raise AuditError("positions_snapshot_contains_invalid_rows")

    # The two reads above prove repeatability of the position-defining fields.
    # The checks below validate row details used by accounting/reconciliation.

    # Hedge mode may legitimately contain LONG and SHORT for one symbol.
    # Only an exact duplicate (symbol, side) pair is rejected.
    seen_position_keys = set()

    for index, row in enumerate(rows):
        symbol = row.get("symbol")

        if not isinstance(symbol, str) or not symbol.strip():
            raise AuditError(f"position_symbol_missing:{index}")

        side = row.get("side")

        if side not in ("LONG", "SHORT"):
            raise AuditError(f"position_side_invalid:{index}")

        position_key = (symbol, side)

        if position_key in seen_position_keys:
            raise AuditError(
                f"duplicate_position_symbol_side:{symbol}:{side}"
            )

        seen_position_keys.add(position_key)

        quantity = _decimal(
            row.get("position"),
            f"position_{index}_quantity",
        )

        if quantity < 0:
            raise AuditError(f"position_quantity_negative:{index}")

        if quantity > 0:
            average_price = _decimal(
                row.get("avgPrice"),
                f"position_{index}_avgPrice",
            )

            if average_price <= 0:
                raise AuditError(
                    f"position_avgPrice_not_positive:{index}"
                )

    return rows


def _summarize_fills(rows, missing_realized_pnl=0):
    commission_by_asset = defaultdict(Decimal)
    rebate_by_asset = defaultdict(Decimal)
    realized_pnl_usdt = Decimal("0")

    maker_count = 0
    taker_count = 0

    for row in rows:
        asset = row["commission_asset"]
        commission_by_asset[asset] += Decimal(row["commission"])
        rebate_by_asset[asset] += Decimal(row["maker_rebate"])
        realized_pnl_usdt += Decimal(row["realized_pnl"])

        if row.get("is_maker") is True:
            maker_count += 1
        elif row.get("is_maker") is False:
            taker_count += 1

    return {
        "fill_count": len(rows),
        "maker_fill_count": maker_count,
        "taker_fill_count": taker_count,
        "commission_by_asset": {
            key: str(value)
            for key, value in sorted(commission_by_asset.items())
        },
        "maker_rebate_by_asset": {
            key: str(value)
            for key, value in sorted(rebate_by_asset.items())
        },
        "net_commission_by_asset": {
            key: str(commission_by_asset[key] - rebate_by_asset[key])
            for key in sorted(set(commission_by_asset) | set(rebate_by_asset))
        },
        "reported_realized_pnl_usdt": str(realized_pnl_usdt),
        "realized_pnl_missing_fill_count": missing_realized_pnl,
        "realized_pnl_complete": missing_realized_pnl == 0,
    }


def _read_stable_balance_flow(
    client,
    *,
    page_size=1000,
    max_pages=100,
):
    """Read and verify the exchange's available futures flow history."""
    try:
        rows = fetch_all_futures_balance_flow(
            client,
            page_size=page_size,
            max_pages=max_pages,
            verify_stable=True,
        )
        summary = summarize_balance_flow(rows)
    except (BalanceFlowError, ValueError) as exc:
        raise AuditError(
            f"balance_flow_audit_failed:{str(exc)[:160]}"
        ) from None

    return rows, summary


def build_report(
    client,
    symbols,
    ledger_path,
    page_size=1000,
    max_pages=100,
):
    if not isinstance(symbols, list) or not symbols:
        raise AuditError("symbols_must_be_nonempty_list")

    if len(symbols) != len(set(symbols)):
        raise AuditError("duplicate_symbols")

    raw_by_symbol = {}
    pagination = {}

    # 1. Complete trade history for every configured symbol.
    for symbol in symbols:
        rows = fetch_complete_user_trades_v2(
            client,
            symbol,
            page_size=page_size,
            max_pages=max_pages,
        )

        raw_by_symbol[symbol] = rows
        pagination[symbol] = {
            "complete": True,
            "trade_count": len(rows),
            "first_trade_id": rows[0].get("id") if rows else None,
            "last_trade_id": rows[-1].get("id") if rows else None,
        }

    # 2. Account snapshots and stable balance-flow history.
    positions = _read_positions(client)

    try:
        open_orders = fetch_all_open_orders(
            client,
            limit=1000,
            max_pages=100,
            verify_stable=True,
        )
    except (OpenOrdersReadError, ValueError):
        raise AuditError(
            "stable_open_orders_snapshot_not_proven"
        ) from None

    try:
        open_algo_orders = fetch_all_open_algo_orders(
            client,
            limit=1000,
            max_pages=100,
            verify_stable=True,
        )
    except (OpenAlgoOrdersReadError, ValueError):
        raise AuditError(
            "stable_open_algo_orders_snapshot_not_proven"
        ) from None

    flow_rows, flow_summary = _read_stable_balance_flow(
        client,
        page_size=page_size,
        max_pages=max_pages,
    )

    try:
        commission_rates = {
            symbol: fetch_stable_commission_rates(client, symbol)
            for symbol in symbols
        }
    except (CommissionRateError, ValueError) as exc:
        raise AuditError(
            f"commission_rate_audit_failed:{str(exc)[:120]}"
        ) from None

    all_raw = [
        row
        for symbol in symbols
        for row in raw_by_symbol[symbol]
    ]

    missing_realized = sum(
        1 for row in all_raw
        if row.get("realizedPnl") is None
    )

    # 3. Append validated fills only after all remote reads succeeded.
    added, skipped = append_user_trades(
        str(ledger_path),
        all_raw,
    )

    ledger_rows = load_fill_ledger(str(ledger_path))

    accounting = _summarize_fills(
        ledger_rows,
        missing_realized_pnl=missing_realized,
    )

    active_positions = []

    for row in positions:
        quantity = Decimal(str(row["position"]))

        if quantity > 0:
            active_positions.append({
                "symbol": row["symbol"],
                "side": row["side"],
                "position": str(quantity),
                "avgPrice": str(row["avgPrice"]),
                "markPrice": str(row.get("markPrice", "")),
                "unrealizedPnL": _position_unrealized_pnl(row),
            })

    blockers = []

    if not accounting["realized_pnl_complete"]:
        blockers.append(
            "realized_pnl_missing_on_one_or_more_fills"
        )

    if not flow_summary["known_flow_categories_complete"]:
        blockers.append(
            "unknown_balance_flow_types_present"
        )

    # These blockers remain until the relevant architecture is implemented.
    blockers.append("durable_order_intent_journal_not_connected")
    blockers.append(
        "internal_v2_position_state_not_automatically_compared"
    )

    normalized_flow_rows = [
        {
            "id": row["id"],
            "coin": row["coin"],
            "symbol": row["symbol"],
            "flow_type": row["flow_type_raw"],
            "flow_type_value": row["flow_type_value"],
            "category": row["category"],
            "change": row["change"],
            "total": row["total"],
            "created_ms": row["created_ms"],
        }
        for row in flow_rows
    ]

    return {
        "schema": "apexquant_toobit_full_account_trade_audit_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "symbols": symbols,
        "symbols_checked": len(symbols),
        "pagination": pagination,
        "fill_ledger": {
            "path": str(ledger_path),
            "scope": "per_run_artifact",
            "rows_before_this_run": len(ledger_rows) - added,
            "new_rows": added,
            "exact_duplicates_skipped": skipped,
            "rows_after_this_run": len(ledger_rows),
            "durable_across_workflow_runs": False,
        },
        "accounting": accounting,
        "commission_rates": {
            "endpoint": "/api/v1/futures/commissionRate",
            "stable_snapshots_verified": True,
            "by_symbol": commission_rates,
            "used_for_historical_fill_repricing": False,
            "note": (
                "Rates are current account rates for future cost estimates; "
                "historical fills retain their exchange-reported commissions."
            ),
        },
        "balance_flow": {
            "endpoint": "/api/v1/futures/balanceFlow",
            "scope": BALANCE_FLOW_SCOPE,
            "stable_snapshot_verified": True,
            "row_count": len(normalized_flow_rows),
            "summary": flow_summary,
            "rows": normalized_flow_rows,
            "all_time_history_claimed": False,
            "note": (
                "Balance-flow totals are reported separately from "
                "fill-ledger totals. Do not add them together without "
                "matching time windows, assets, and accounting semantics."
            ),
        },
        "account_snapshot": {
            "positions_snapshot_complete": True,
            "positions_snapshot_stable": True,
            "active_positions": active_positions,
            "active_position_count": len(active_positions),
            "open_orders_snapshot_complete_and_stable": True,
            "open_orders": open_orders,
            "open_order_count": len(open_orders),
            "open_algo_orders_snapshot_complete_and_stable": True,
            "open_algo_orders": open_algo_orders,
            "open_algo_order_count": len(open_algo_orders),
        },
        "order_intent_journal_present": False,
        "reconciliation": {
            "fill_history_complete_for_configured_symbols": True,
            "balance_flow_snapshot_stable": True,
            "balance_flow_known_categories_complete": (
                flow_summary["known_flow_categories_complete"]
            ),
            "positions_snapshot_received": True,
            "open_orders_snapshot_stable": True,
            "open_algo_orders_snapshot_stable": True,
            "internal_position_comparison": False,
            "unresolved_intent_comparison": False,
            "ok": False,
            "halt_trading": True,
            "blockers": blockers,
        },
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "live_execution_enabled": False,
        "live_authorized": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--whitelist",
        default=str(ROOT / "config/crypto_whitelist.json"),
    )
    parser.add_argument(
        "--ledger",
        default="reports/toobit_fill_ledger.jsonl",
    )
    parser.add_argument(
        "--out",
        default="reports/toobit_full_account_trade_audit.json",
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)

    args = parser.parse_args(argv)

    if not 1 <= args.page_size <= 1000 or args.max_pages < 1:
        print(json.dumps({
            "ok": False,
            "error": "invalid_pagination_limits",
            "read_only": True,
        }))
        return 2

    if (
        not os.environ.get("TOOBIT_API_KEY")
        or not os.environ.get("TOOBIT_API_SECRET")
    ):
        print(json.dumps({
            "ok": False,
            "error": "missing_api_credentials",
            "read_only": True,
        }))
        return 2

    try:
        symbols = load_symbols(Path(args.whitelist))
        client = ToobitClient()

        report = build_report(
            client,
            symbols,
            Path(args.ledger),
            page_size=args.page_size,
            max_pages=args.max_pages,
        )

        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print(json.dumps({
            "schema": report["schema"],
            "read_only": True,
            "symbols_checked": report["symbols_checked"],
            "fills": report["accounting"]["fill_count"],
            "commission_rate_symbols_checked": len(
                report["commission_rates"]["by_symbol"]
            ),
            "balance_flow_rows": report["balance_flow"]["row_count"],
            "balance_flow_scope": report["balance_flow"]["scope"],
            "unknown_flow_type_count": (
                report["balance_flow"]["summary"][
                    "unknown_flow_type_count"
                ]
            ),
            "active_positions": (
                report["account_snapshot"]["active_position_count"]
            ),
            "open_orders": (
                report["account_snapshot"]["open_order_count"]
            ),
            "reconciliation_ok": False,
            "halt_trading": True,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "live_execution_enabled": False,
            "live_authorized": False,
            "report_path": str(output),
        }, indent=2))

        return 0

    except (
        AuditError,
        TradePaginationError,
        BalanceFlowError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(json.dumps({
            "ok": False,
            "read_only": True,
            "halt_trading": True,
            "error": str(exc)[:240],
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "live_execution_enabled": False,
            "live_authorized": False,
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
