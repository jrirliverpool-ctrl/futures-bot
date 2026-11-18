
"""Read-only, fail-closed Toobit account reconciliation coordinator.

Checks:
- stable account-wide V1 position snapshot;
- stable V2 STOP and STOP_PROFIT_LOSS algo-order snapshot;
- internal-vs-exchange position reconciliation;
- one correctly sized protective STOP_LOSS per active position;
- unresolved order-intent recovery.

No order is submitted, amended, or canceled.
No journal, ledger, or trading state is mutated.
"""
import re
from decimal import Decimal, InvalidOperation

from order_recovery_probe import OrderRecoveryProbe
from position_reconciliation import reconcile_position_snapshot
from toobit_position_snapshot import (
    PositionSnapshotError,
    fetch_stable_position_snapshot,
)
from toobit_v2_algo_snapshot import (
    AlgoOrderSnapshotError,
    fetch_stable_v2_algo_order_snapshot,
)
from toobit_protection_reconciliation import reconcile_protective_orders


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+-SWAP-USDT$")


def _decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is missing or invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} is not numeric") from None
    if not number.is_finite():
        raise ValueError(f"{field} is not finite")
    return number


class ToobitReadOnlyReconciliation:
    """Coordinate read-only order, position and protection checks."""

    def __init__(
        self,
        client,
        intent_store,
        symbols,
        expected_positions,
        trade_limit=1000,
    ):
        if client is None or not callable(getattr(client, "_request", None)):
            raise ValueError("client must provide _request")
        if intent_store is None:
            raise ValueError("intent_store is required")
        if not isinstance(symbols, (list, tuple)) or not symbols:
            raise ValueError("symbols must be a non-empty list")

        normalized_symbols = []
        for symbol in symbols:
            if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
                raise ValueError("invalid USDT-M futures symbol")
            if symbol in normalized_symbols:
                raise ValueError("duplicate symbol")
            normalized_symbols.append(symbol)

        if not isinstance(expected_positions, dict):
            raise ValueError("expected_positions must be a dictionary")
        if set(expected_positions) != set(normalized_symbols):
            raise ValueError(
                "expected_positions must contain exactly the configured symbols"
            )

        self.client = client
        self.intent_store = intent_store
        self.symbols = tuple(normalized_symbols)
        self.expected_positions = dict(expected_positions)
        self.trade_limit = trade_limit
        self.recovery_probe = OrderRecoveryProbe(
            client,
            intent_store,
            trade_limit=trade_limit,
        )

    def run(self, now_ms=None):
        """Run the complete read-only reconciliation."""

        # 1. Obtain a stable account-wide position snapshot.
        try:
            positions = fetch_stable_position_snapshot(self.client)
        except PositionSnapshotError as exc:
            return self._failure(
                "positions_snapshot_not_stable",
                detail=str(exc),
            )
        except Exception as exc:
            return self._failure(
                "positions_snapshot_failed",
                detail=type(exc).__name__,
            )

        if not isinstance(positions, list):
            return self._failure(
                "positions_response_not_list",
                detail="The exchange response was not a complete position list.",
            )
        if not all(isinstance(row, dict) for row in positions):
            return self._failure("positions_response_contains_invalid_rows")

        unexpected_active = []
        for index, row in enumerate(positions):
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
                return self._failure(
                    "positions_snapshot_symbol_invalid",
                    detail=f"Invalid symbol at row {index}.",
                )
            try:
                quantity = _decimal(
                    row.get("position"),
                    f"positions[{index}].position",
                )
            except ValueError as exc:
                return self._failure(
                    "positions_snapshot_quantity_invalid",
                    detail=str(exc),
                )
            if quantity < 0:
                return self._failure(
                    "positions_snapshot_negative_quantity",
                    detail=f"Negative quantity for {symbol}.",
                )
            if quantity > 0 and symbol not in self.symbols:
                unexpected_active.append(symbol)

        if unexpected_active:
            return self._failure(
                "active_positions_outside_configured_universe",
                detail=", ".join(sorted(set(unexpected_active))),
            )

        # 2. Compare internal expected positions with exchange positions.
        position_reports = {}
        for symbol in self.symbols:
            try:
                position_reports[symbol] = reconcile_position_snapshot(
                    symbol,
                    self.expected_positions[symbol],
                    positions,
                )
            except Exception as exc:
                return self._failure(
                    "position_reconciliation_failed",
                    detail=type(exc).__name__,
                    position_reports=position_reports,
                )

        positions_ok = all(
            report.get("ok") is True
            and report.get("halt_trading") is False
            for report in position_reports.values()
        )

        # 3. Read both V2 algo categories twice and require a stable snapshot.
        try:
            algo_snapshot = fetch_stable_v2_algo_order_snapshot(
                self.client,
                symbol=None,
                limit=1000,
            )
        except AlgoOrderSnapshotError as exc:
            return self._failure(
                "algo_orders_snapshot_not_stable",
                detail=str(exc),
                positions_ok=positions_ok,
                position_reports=position_reports,
                exchange_positions_snapshot_received=True,
                exchange_positions_snapshot_stable=True,
            )
        except Exception as exc:
            return self._failure(
                "algo_orders_snapshot_failed",
                detail=type(exc).__name__,
                positions_ok=positions_ok,
                position_reports=position_reports,
                exchange_positions_snapshot_received=True,
                exchange_positions_snapshot_stable=True,
            )

        # 4. Verify one unambiguous full-size STOP_LOSS for every open side.
        protection_report = reconcile_protective_orders(
            positions,
            algo_snapshot,
            list(self.symbols),
        )
        protection_ok = (
            protection_report.get("ok") is True
            and protection_report.get("halt_trading") is False
        )

        # 5. Recover unresolved order intents; this probe is read-only.
        try:
            order_report = self.recovery_probe.run(now_ms=now_ms)
        except Exception as exc:
            return self._failure(
                "order_recovery_probe_failed",
                detail=type(exc).__name__,
                positions_ok=positions_ok,
                protection_ok=protection_ok,
                position_reports=position_reports,
                protective_orders_report=protection_report,
                exchange_positions_snapshot_received=True,
                exchange_positions_snapshot_stable=True,
                exchange_algo_orders_snapshot_received=True,
                exchange_algo_orders_snapshot_stable=True,
            )

        orders_ok = (
            isinstance(order_report, dict)
            and order_report.get("ok") is True
            and order_report.get("halt_trading") is False
            and order_report.get("unresolved_count") == 0
        )

        all_ok = positions_ok and protection_ok and orders_ok

        return {
            "ok": all_ok,
            "halt_trading": not all_ok,
            "positions_ok": positions_ok,
            "protection_ok": protection_ok,
            "orders_ok": orders_ok,
            "symbols_checked": list(self.symbols),
            "position_reports": position_reports,
            "protective_orders_report": protection_report,
            "order_recovery_report": order_report,
            "exchange_positions_snapshot_received": True,
            "exchange_positions_snapshot_stable": True,
            "exchange_algo_orders_snapshot_received": True,
            "exchange_algo_orders_snapshot_stable": True,
            "read_only": True,
            "network_methods_used": ["GET"],
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "journal_mutated": False,
            "fill_ledger_mutated": False,
            "trading_state_mutated": False,
            "manual_review_required": not all_ok,
        }

    @staticmethod
    def _failure(reason, detail=None, **extra):
        result = {
            "ok": False,
            "halt_trading": True,
            "positions_ok": False,
            "protection_ok": False,
            "orders_ok": False,
            "reason": reason,
            "detail": detail,
            "exchange_positions_snapshot_received": False,
            "exchange_positions_snapshot_stable": False,
            "exchange_algo_orders_snapshot_received": False,
            "exchange_algo_orders_snapshot_stable": False,
            "read_only": True,
            "network_methods_used": ["GET"],
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "journal_mutated": False,
            "fill_ledger_mutated": False,
            "trading_state_mutated": False,
            "manual_review_required": True,
        }
        result.update(extra)
        return result
