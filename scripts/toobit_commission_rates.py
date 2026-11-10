"""Read and verify per-symbol Toobit commission rates without trading."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

ENDPOINT = "/api/v1/futures/commissionRate"
FEE_FIELDS = (
    "openMakerFee",
    "openTakerFee",
    "closeMakerFee",
    "closeTakerFee",
)


class CommissionRateError(RuntimeError):
    """A complete, valid, repeatable commission-rate read failed."""


def _normalize(payload):
    if isinstance(payload, dict) and payload.get("_transport"):
        raise CommissionRateError("commission_rate_transport_unknown")
    if not isinstance(payload, dict):
        raise CommissionRateError("commission_rate_response_not_object")

    if payload.get("code") is not None and payload.get("code") not in (
        0, 200, "0", "200"
    ):
        raise CommissionRateError("commission_rate_exchange_error")

    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise CommissionRateError("commission_rate_data_not_object")

    result = {}
    for field in FEE_FIELDS:
        value = data.get(field)
        if isinstance(value, bool) or value is None:
            raise CommissionRateError("commission_rate_field_missing_or_invalid")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise CommissionRateError("commission_rate_field_not_numeric") from None
        if not number.is_finite() or number < 0:
            raise CommissionRateError("commission_rate_field_out_of_range")
        result[field] = str(number)

    return result


def fetch_stable_commission_rates(client, symbol):
    """Read a symbol's maker/taker open/close rates twice and require equality."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be non-empty text")
    if client is None or not callable(getattr(client, "_request", None)):
        raise CommissionRateError("commission_rate_client_invalid")

    snapshots = []
    for _ in range(2):
        try:
            payload = client._request("GET", ENDPOINT, {"symbol": symbol})
        except Exception:
            raise CommissionRateError("commission_rate_request_failed") from None
        snapshots.append(_normalize(payload))

    if snapshots[0] != snapshots[1]:
        raise CommissionRateError("commission_rate_changed_between_reads")

    rates = snapshots[1]
    rates["symbol"] = symbol
    rates["round_trip_taker_rate"] = str(
        Decimal(rates["openTakerFee"]) + Decimal(rates["closeTakerFee"])
    )
    rates["stable_snapshot_verified"] = True
    return rates
