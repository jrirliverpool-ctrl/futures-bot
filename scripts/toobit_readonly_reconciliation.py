
"""Read-only account reconciliation coordinator for Toobit USDT-M futures.

Combines:
- durable unresolved order-intent recovery probe;
- one account-wide Toobit positions snapshot;
- per-symbol comparison against caller-supplied internal positions.

This module never submits, amends, or cancels orders.
It never mutates the intent journal, fill ledger, or trading state.
Any incomplete or ambiguous result halts trading.
"""

import re
from decimal import Decimal, InvalidOperation

from order_recovery_probe import OrderRecoveryProbe
from position_reconciliation import reconcile_position_snapshot
from toobit_position_snapshot import (
    PositionSnapshotError,
    fetch_stable_position_snapshot,
)


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
    """Coordinate read-only order and position reconciliation."""

    def __init__(
        self,
        client,
        intent_store,
        symbols,
        expected_positions,
        trade_limit=1000,
    ):
        if client is None or not callable(
            getattr(client, "_request", None)
        ):
            raise ValueError("client must provide _request")

        if intent_store is None:
            raise ValueError("intent_store is required")

        if not isinstance(symbols, (list, tuple)) or not symbols:
            raise ValueError("symbols must be a non-empty list")

        normalized_symbols = []

        for symbol in symbols:
            if (
                not isinstance(symbol, str)
                or not SYMBOL_PATTERN.fullmatch(symbol)
            ):
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

        # Validate the trade limit through the existing recovery probe.
        self.recovery_probe = OrderRecoveryProbe(
            client,
            intent_store,
            trade_limit=trade_limit,
        )

    def run(self, now_ms=None):
        """Run a read-only account reconciliation report."""

        try:
            positions = fetch_stable_position_snapshot(self.client)
        except PositionSnapshotError as exc:
            return self._failure(
                "positions_snapshot_not_stable",
                detail=str(exc),
            )

        if not isinstance(positions, list):
            return self._failure(
                "positions_response_not_list",
                detail="The exchange response was not a complete position list.",
            )

        if not all(isinstance(row, dict) for row in positions):
            return self._failure(
                "positions_response_contains_invalid_rows"
            )

        # Validate the complete account snapshot, including positions
        # outside the configured strategy universe.
        unexpected_active = []

        for index, row in enumerate(positions):
            symbol = row.get("symbol")

            if (
                not isinstance(symbol, str)
                or not SYMBOL_PATTERN.fullmatch(symbol)
            ):
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

        position_reports = {}

        for symbol in self.symbols:
            result = reconcile_position_snapshot(
                symbol,
                self.expected_positions[symbol],
                positions,
            )

            position_reports[symbol] = result

        try:
            order_report = self.recovery_probe.run(now_ms=now_ms)
        except Exception as exc:
            return self._failure(
                "order_recovery_probe_failed",
                detail=type(exc).__name__,
                position_reports=position_reports,
            )

        positions_ok = all(
            report.get("ok") is True
            and report.get("halt_trading") is False
            for report in position_reports.values()
        )

        orders_ok = (
            order_report.get("ok") is True
            and order_report.get("halt_trading") is False
            and order_report.get("unresolved_count") == 0
        )

        all_ok = positions_ok and orders_ok

        return {
            "ok": all_ok,
            "halt_trading": not all_ok,
            "positions_ok": positions_ok,
            "orders_ok": orders_ok,
            "symbols_checked": list(self.symbols),
            "position_reports": position_reports,
            "order_recovery_report": order_report,
            "exchange_positions_snapshot_received": True,
            "exchange_positions_snapshot_stable": True,
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
            "orders_ok": False,
            "reason": reason,
            "detail": detail,
            "exchange_positions_snapshot_received": False,
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
