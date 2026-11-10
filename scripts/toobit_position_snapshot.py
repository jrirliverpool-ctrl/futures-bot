"""Repeatability check for read-only Toobit futures position snapshots."""

from decimal import Decimal, InvalidOperation


POSITIONS_ENDPOINT = "/api/v1/futures/positions"


class PositionSnapshotError(RuntimeError):
    """Raised when a complete, repeatable position list cannot be proven."""


def _signature(rows):
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PositionSnapshotError("positions_response_not_list_of_objects")

    positions = {}
    for index, row in enumerate(rows):
        symbol = row.get("symbol")
        side = row.get("side")
        if not isinstance(symbol, str) or not symbol.strip():
            raise PositionSnapshotError(f"position_symbol_invalid:{index}")
        if side not in ("LONG", "SHORT"):
            raise PositionSnapshotError(f"position_side_invalid:{index}")
        key = (symbol.strip(), side)
        if key in positions:
            raise PositionSnapshotError(f"duplicate_position_side:{symbol}:{side}")
        try:
            quantity = Decimal(str(row.get("position")))
        except (InvalidOperation, TypeError, ValueError):
            raise PositionSnapshotError(f"position_quantity_invalid:{index}") from None
        if not quantity.is_finite() or quantity < 0:
            raise PositionSnapshotError(f"position_quantity_invalid:{index}")
        if quantity == 0:
            continue
        try:
            average_price = Decimal(str(row.get("avgPrice")))
        except (InvalidOperation, TypeError, ValueError):
            raise PositionSnapshotError(f"position_average_price_invalid:{index}") from None
        if not average_price.is_finite() or average_price <= 0:
            raise PositionSnapshotError(f"position_average_price_invalid:{index}")

        leverage = row.get("leverage")
        if leverage is not None:
            try:
                leverage = Decimal(str(leverage))
            except (InvalidOperation, TypeError, ValueError):
                raise PositionSnapshotError(f"position_leverage_invalid:{index}") from None
            if not leverage.is_finite() or leverage <= 0:
                raise PositionSnapshotError(f"position_leverage_invalid:{index}")

        margin_type = row.get("marginType")
        if margin_type is not None and margin_type not in ("CROSS", "ISOLATED", "CROSSED"):
            raise PositionSnapshotError(f"position_margin_type_invalid:{index}")
        positions[key] = (quantity, average_price, leverage, margin_type)

    return positions


def fetch_stable_position_snapshot(client):
    """Return the second GET only when two consecutive core snapshots agree.

    Mark price, unrealized PnL, and margin values are intentionally excluded:
    they can move between requests without a position change. Active side,
    quantity, entry price, leverage, and margin mode must remain stable.
    """
    snapshots = []
    signatures = []
    for _ in range(2):
        try:
            rows = client._request("GET", POSITIONS_ENDPOINT, {})
        except Exception as exc:
            raise PositionSnapshotError(
                f"positions_request_failed:{type(exc).__name__}"
            ) from None
        signatures.append(_signature(rows))
        snapshots.append(rows)

    if signatures[0] != signatures[1]:
        raise PositionSnapshotError("positions_snapshot_changed_between_reads")
    return snapshots[1]
