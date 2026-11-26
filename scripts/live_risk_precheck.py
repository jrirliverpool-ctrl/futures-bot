
"""Conservative pre-trade risk and contract-quantity validation.

This module performs local validation only.
It never submits, modifies, or cancels exchange orders.

All quantities are expressed in exchange contracts, not underlying tokens.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any


HARD_RISK_FRACTION_CAP = Decimal("0.01")
HARD_LEVERAGE_CAP = Decimal("3")
HARD_MARGIN_UTILIZATION_CAP = Decimal("0.50")


class RiskInputError(ValueError):
    """Raised for invalid risk-precheck input."""


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise RiskInputError(f"{field}_invalid")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RiskInputError(f"{field}_invalid") from None

    if not result.is_finite():
        raise RiskInputError(f"{field}_invalid")

    return result


def _positive(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)

    if result <= 0:
        raise RiskInputError(f"{field}_must_be_positive")

    return result


def _non_negative(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)

    if result < 0:
        raise RiskInputError(f"{field}_must_be_non_negative")

    return result


def _floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise RiskInputError("qty_step_must_be_positive")

    units = (quantity / step).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return units * step


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _result(
    *,
    eligible: bool,
    blockers: list[str],
    quantity: Decimal = Decimal("0"),
    estimated_loss: Decimal = Decimal("0"),
    margin_required: Decimal = Decimal("0"),
    margin_utilization: Decimal = Decimal("0"),
    entry_notional: Decimal = Decimal("0"),
    entry_cost: Decimal = Decimal("0"),
    exit_cost: Decimal = Decimal("0"),
    loss_per_contract: Decimal = Decimal("0"),
    quantity_unit: str = "contracts",
) -> dict:
    return {
        "eligible": bool(eligible),
        "blockers": list(dict.fromkeys(blockers)),
        "quantity": str(quantity),
        "quantity_unit": quantity_unit,
        "estimated_stop_loss_and_cost_usdt": str(estimated_loss),
        "estimated_stop_loss_per_contract_usdt": str(loss_per_contract),
        "estimated_entry_cost_usdt": str(entry_cost),
        "estimated_exit_cost_usdt": str(exit_cost),
        "estimated_round_trip_cost_usdt": str(entry_cost + exit_cost),
        "entry_notional_usdt": str(entry_notional),
        "estimated_margin_required_usdt": str(margin_required),
        "margin_utilization": str(margin_utilization),
        "orders_submitted": 0,
    }


def precheck_order(
    *,
    symbol,
    side,
    entry_price,
    stop_price,
    available_usdt,
    risk_fraction,
    leverage,
    contract_specs,
    fee_rate_per_side,
    slippage_rate_per_side,
    risk_fraction_cap="0.01",
    leverage_cap="3",
    margin_utilization_cap="0.50",
):
    """Validate an order's estimated risk and contract quantity.

    Risk is estimated using:
      - entry notional for entry fees and entry slippage;
      - stop-price notional for exit fees and exit slippage;
      - contract multiplier;
      - available USDT and requested risk fraction.

    The returned quantity is always in exchange contracts.
    """

    blockers: list[str] = []

    # ---------------------------------------------------------
    # Basic shape and identity
    # ---------------------------------------------------------

    if not isinstance(contract_specs, dict):
        return _result(
            eligible=False,
            blockers=["contract_specs_invalid"],
        )

    if not isinstance(symbol, str) or not symbol.strip():
        blockers.append("symbol_invalid")

    if side not in {"LONG", "SHORT"}:
        blockers.append("side_invalid")

    if blockers:
        return _result(eligible=False, blockers=blockers)

    symbol = symbol.strip()

    if not symbol.endswith("-SWAP-USDT"):
        blockers.append("symbol_not_usdt_swap")

    if contract_specs.get("symbol") != symbol:
        blockers.append("symbol_specs_mismatch")

    if contract_specs.get("status") != "TRADING":
        blockers.append("contract_not_trading")

    if contract_specs.get("quantity_unit") != "contracts":
        blockers.append("quantity_unit_must_be_contracts")

    if contract_specs.get("min_notional_verified") is not True:
        blockers.append("min_notional_unverified")

    # ---------------------------------------------------------
    # Parse numeric inputs
    # ---------------------------------------------------------

    try:
        entry = _positive(entry_price, "entry_price")
    except RiskInputError:
        blockers.append("entry_price_invalid")
        entry = None

    try:
        stop = _positive(stop_price, "stop_price")
    except RiskInputError:
        blockers.append("stop_price_invalid")
        stop = None

    try:
        wallet = _positive(available_usdt, "available_usdt")
    except RiskInputError:
        blockers.append("available_usdt_invalid")
        wallet = None

    try:
        risk = _positive(risk_fraction, "risk_fraction")
    except RiskInputError:
        blockers.append("risk_fraction_invalid")
        risk = None

    try:
        lev = _positive(leverage, "leverage")
    except RiskInputError:
        blockers.append("leverage_invalid")
        lev = None

    try:
        fee = _non_negative(fee_rate_per_side, "fee_rate")
    except RiskInputError:
        blockers.append("fee_rate_invalid")
        fee = None

    try:
        slippage = _non_negative(
            slippage_rate_per_side,
            "slippage_rate",
        )
    except RiskInputError:
        blockers.append("slippage_rate_invalid")
        slippage = None

    try:
        requested_risk_cap = _positive(
            risk_fraction_cap,
            "risk_fraction_cap",
        )
    except RiskInputError:
        blockers.append("risk_fraction_cap_invalid")
        requested_risk_cap = None

    try:
        requested_leverage_cap = _positive(
            leverage_cap,
            "leverage_cap",
        )
    except RiskInputError:
        blockers.append("leverage_cap_invalid")
        requested_leverage_cap = None

    try:
        requested_margin_cap = _positive(
            margin_utilization_cap,
            "margin_utilization_cap",
        )
    except RiskInputError:
        blockers.append("margin_utilization_cap_invalid")
        requested_margin_cap = None

    # ---------------------------------------------------------
    # Contract specifications
    # ---------------------------------------------------------

    spec_values = {}

    for key in (
        "contract_multiplier",
        "min_qty",
        "max_qty",
        "qty_step",
        "min_notional",
        "max_leverage",
    ):
        try:
            spec_values[key] = _positive(
                contract_specs.get(key),
                key,
            )
        except RiskInputError:
            blockers.append(f"{key}_invalid")

    if blockers:
        return _result(
            eligible=False,
            blockers=blockers,
        )

    multiplier = spec_values["contract_multiplier"]
    min_qty = spec_values["min_qty"]
    max_qty = spec_values["max_qty"]
    qty_step = spec_values["qty_step"]
    min_notional = spec_values["min_notional"]
    exchange_max_leverage = spec_values["max_leverage"]

    if max_qty < min_qty:
        blockers.append("max_qty_below_min_qty")

    # ---------------------------------------------------------
    # Hard policy limits
    # ---------------------------------------------------------

    if requested_risk_cap > HARD_RISK_FRACTION_CAP:
        blockers.append("risk_fraction_cap_exceeds_hard_cap")

    if risk > HARD_RISK_FRACTION_CAP:
        blockers.append("risk_fraction_exceeds_hard_cap")

    if requested_risk_cap is not None and risk is not None:
        if risk > requested_risk_cap:
            blockers.append("risk_fraction_exceeds_configured_cap")

    if requested_leverage_cap > HARD_LEVERAGE_CAP:
        blockers.append("leverage_cap_exceeds_hard_cap")

    if lev > HARD_LEVERAGE_CAP:
        blockers.append("leverage_exceeds_hard_cap")

    if lev > requested_leverage_cap:
        blockers.append("leverage_exceeds_configured_cap")

    if lev > exchange_max_leverage:
        blockers.append("leverage_exceeds_exchange_limit")

    if requested_margin_cap > HARD_MARGIN_UTILIZATION_CAP:
        blockers.append("margin_cap_exceeds_hard_cap")

    # ---------------------------------------------------------
    # Stop direction
    # ---------------------------------------------------------

    if entry is not None and stop is not None:
        if side == "LONG" and stop >= entry:
            blockers.append("long_stop_must_be_below_entry")

        if side == "SHORT" and stop <= entry:
            blockers.append("short_stop_must_be_above_entry")

    if blockers:
        return _result(
            eligible=False,
            blockers=blockers,
        )

    # ---------------------------------------------------------
    # Cost-aware risk per contract
    # ---------------------------------------------------------

    assert entry is not None
    assert stop is not None
    assert wallet is not None
    assert risk is not None
    assert lev is not None
    assert fee is not None
    assert slippage is not None

    rate_per_side = fee + slippage

    entry_notional_per_contract = entry * multiplier
    exit_notional_per_contract = stop * multiplier

    entry_cost_per_contract = (
        entry_notional_per_contract * rate_per_side
    )

    exit_cost_per_contract = (
        exit_notional_per_contract * rate_per_side
    )

    round_trip_cost_per_contract = (
        entry_cost_per_contract + exit_cost_per_contract
    )

    price_loss_per_contract = (
        abs(entry - stop) * multiplier
    )

    loss_per_contract = (
        price_loss_per_contract + round_trip_cost_per_contract
    )

    if loss_per_contract <= 0:
        return _result(
            eligible=False,
            blockers=["estimated_loss_per_contract_invalid"],
        )

    risk_budget = wallet * risk

    raw_quantity = risk_budget / loss_per_contract
    quantity = _floor_to_step(raw_quantity, qty_step)

    if quantity < min_qty:
        blockers.append("quantity_below_minimum")

    if quantity > max_qty:
        quantity = _floor_to_step(max_qty, qty_step)

    if quantity < min_qty:
        blockers.append("quantity_outside_exchange_limits")

    if blockers:
        return _result(
            eligible=False,
            blockers=blockers,
            quantity=quantity,
            loss_per_contract=loss_per_contract,
        )

    # ---------------------------------------------------------
    # Notional and margin checks
    # ---------------------------------------------------------

    entry_notional = quantity * entry * multiplier

    if entry_notional < min_notional:
        blockers.append("minimum_order_value_not_met")

    margin_required = entry_notional / lev

    margin_utilization = margin_required / wallet

    if margin_utilization > requested_margin_cap:
        blockers.append("margin_utilization_exceeds_cap")

    estimated_loss = quantity * loss_per_contract

    # Final independent risk check.
    if estimated_loss > risk_budget:
        blockers.append("estimated_loss_exceeds_risk_budget")

    if quantity > max_qty:
        blockers.append("quantity_exceeds_exchange_maximum")

    eligible = not blockers

    return _result(
        eligible=eligible,
        blockers=blockers,
        quantity=quantity,
        estimated_loss=estimated_loss,
        margin_required=margin_required,
        margin_utilization=margin_utilization,
        entry_notional=entry_notional,
        entry_cost=quantity * entry_cost_per_contract,
        exit_cost=quantity * exit_cost_per_contract,
        loss_per_contract=loss_per_contract,
    )
