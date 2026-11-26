
"""Repeatability check for read-only Toobit futures position snapshots.

The snapshot is trusted only when two consecutive GET responses agree on:
- symbol and position side;
- total position quantity;
- available quantity to close;
- average entry price;
- leverage;
- margin mode.

Both position and available quantities are expressed in contracts.
No order is submitted and no state is mutated.
"""

from decimal import Decimal, InvalidOperation


POSITIONS_ENDPOINT = "/api/v1/futures/positions"


class PositionSnapshotError(RuntimeError):
    """Raised when a complete, repeatable position list cannot be proven."""


def _to_decimal(value, field, index):
    if isinstance(value, bool) or value is None:
        raise PositionSnapshotError(
            f"{field}_missing_or_invalid:{index}"
        )

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PositionSnapshotError(
            f"{field}_not_numeric:{index}"
        ) from None

    if not result.is_finite():
        raise PositionSnapshotError(
            f"{field}_not_finite:{index}"
        )

    return result


def _signature(rows):
    """Build a normalized signature for all active position sides."""

    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise PositionSnapshotError(
            "positions_response_not_list_of_objects"
        )

    positions = {}

    for index, row in enumerate(rows):
        symbol = row.get("symbol")
        side = row.get("side")

        if not isinstance(symbol, str) or not symbol.strip():
            raise PositionSnapshotError(
                f"position_symbol_invalid:{index}"
            )

        if side not in ("LONG", "SHORT"):
            raise PositionSnapshotError(
                f"position_side_invalid:{index}"
            )

        symbol = symbol.strip()
        key = (symbol, side)

        if key in positions:
            raise PositionSnapshotError(
                f"duplicate_position_side:{symbol}:{side}"
            )

        quantity = _to_decimal(
            row.get("position"),
            "position_quantity",
            index,
        )

        if quantity < 0:
            raise PositionSnapshotError(
                f"position_quantity_negative:{index}"
            )

        raw_available = row.get("available")

        if raw_available is None:
            available = None
        else:
            available = _to_decimal(
                raw_available,
                "position_available",
                index,
            )

            if available < 0:
                raise PositionSnapshotError(
                    f"position_available_negative:{index}"
                )

        # A flat row is not an active position. Check its own
        # consistency before applying the active-position bound.
        if quantity == 0:
            if available is not None and available != 0:
                raise PositionSnapshotError(
                    f"flat_position_has_nonzero_available:{index}"
                )
            continue

        # For an active position, available is mandatory.
        if available is None:
            raise PositionSnapshotError(
                f"position_available_missing:{index}"
            )

        if available > quantity:
            raise PositionSnapshotError(
                f"position_available_exceeds_position:{index}"
            )

        average_price = _to_decimal(
            row.get("avgPrice"),
            "position_average_price",
            index,
        )

        if average_price <= 0:
            raise PositionSnapshotError(
                f"position_average_price_not_positive:{index}"
            )

        leverage = row.get("leverage")

        if leverage is not None:
            leverage = _to_decimal(
                leverage,
                "position_leverage",
                index,
            )

            if leverage <= 0:
                raise PositionSnapshotError(
                    f"position_leverage_not_positive:{index}"
                )

        margin_type = row.get("marginType")

        if (
            margin_type is not None
            and margin_type not in (
                "CROSS",
                "ISOLATED",
                "CROSSED",
            )
        ):
            raise PositionSnapshotError(
                f"position_margin_type_invalid:{index}"
            )

        positions[key] = (
            quantity,
            available,
            average_price,
            leverage,
            margin_type,
        )

    return positions


def fetch_stable_position_snapshot(client):
    """Return the second GET only when two consecutive core snapshots agree.

    Volatile market fields such as mark price, unrealized PnL and margin
    values are excluded. The available-to-close quantity is included:
    a change in available quantity makes the snapshot unstable.
    """

    if client is None or not callable(
        getattr(client, "_request", None)
    ):
        raise PositionSnapshotError(
            "positions_client_missing_request_method"
        )

    snapshots = []
    signatures = []

    for _ in range(2):
        try:
            rows = client._request(
                "GET",
                POSITIONS_ENDPOINT,
                {},
            )
        except Exception as exc:
            raise PositionSnapshotError(
                f"positions_request_failed:{type(exc).__name__}"
            ) from None

        signatures.append(_signature(rows))
        snapshots.append(rows)

    if signatures[0] != signatures[1]:
        raise PositionSnapshotError(
            "positions_snapshot_changed_between_reads"
        )

    return snapshots[1]
