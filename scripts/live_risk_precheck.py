
"""Pure, fail-closed position sizing precheck.

No network calls and no order submission.
Uses Decimal for deterministic sizing and risk calculations.
"""

from decimal import Decimal, InvalidOperation, ROUND_FLOOR


class RiskInputError(ValueError):
    pass


def D(value, name):
    if isinstance(value, bool) or value is None:
        raise RiskInputError(f"{name}_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RiskInputError(f"{name}_invalid") from None
    if not result.is_finite():
        raise RiskInputError(f"{name}_not_finite")
    return result


def positive(value, name):
    result = D(value, name)
    if result <= 0:
        raise RiskInputError(f"{name}_must_be_positive")
    return result


def non_negative(value, name):
    result = D(value, name)
    if result < 0:
        raise RiskInputError(f"{name}_must_be_non_negative")
    return result


def floor_step(value, step):
    return (
        (value / step).to_integral_value(rounding=ROUND_FLOOR)
        * step
    )


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
    """Return an auditable sizing decision.

    Contract assumptions:
      - contract_multiplier converts contract quantity to notional.
      - fee and slippage rates are charged on entry and exit.
      - stop loss is assumed to execute at stop_price.
      - funding, gaps, liquidation fees and market impact are excluded.

    All rates must be supplied explicitly. No fee defaults are assumed.
    """

    try:
        if not isinstance(contract_specs, dict):
            raise RiskInputError("contract_specs_invalid")

        if not isinstance(symbol, str) or not symbol.endswith("-SWAP-USDT"):
            raise RiskInputError("symbol_invalid")

        if side not in ("LONG", "SHORT"):
            raise RiskInputError("side_invalid")

        if contract_specs.get("symbol") != symbol:
            raise RiskInputError("symbol_specs_mismatch")

        if contract_specs.get("status") != "TRADING":
            raise RiskInputError("contract_not_trading")

        # A new entry requires explicit exchange-confirmed minimum value.
        # Missing verification is treated exactly like failed verification.
        if contract_specs.get("min_notional_verified") is not True:
            raise RiskInputError("min_notional_unverified")

        entry = positive(entry_price, "entry_price")
        stop = positive(stop_price, "stop_price")
        wallet = positive(available_usdt, "available_usdt")
        risk = positive(risk_fraction, "risk_fraction")
        lev = positive(leverage, "leverage")

        fee = non_negative(fee_rate_per_side, "fee_rate")
        slip = non_negative(
            slippage_rate_per_side, "slippage_rate"
        )

        risk_cap = positive(risk_fraction_cap, "risk_cap")
        lev_cap = positive(leverage_cap, "leverage_cap")
        margin_cap = positive(
            margin_utilization_cap, "margin_utilization_cap"
        )

        multiplier = positive(
            contract_specs.get("contract_multiplier"),
            "contract_multiplier",
        )
        min_qty = positive(contract_specs.get("min_qty"), "min_qty")
        max_qty = positive(contract_specs.get("max_qty"), "max_qty")
        step = positive(contract_specs.get("qty_step"), "qty_step")
        min_notional = positive(
            contract_specs.get("min_notional"), "min_notional"
        )
        max_lev = positive(
            contract_specs.get("max_leverage"), "max_leverage"
        )

        if max_qty < min_qty:
            raise RiskInputError("quantity_limits_invalid")

        if margin_cap > Decimal("0.50"):
            raise RiskInputError("margin_cap_exceeds_50_percent")

        if risk > risk_cap or risk_cap > Decimal("0.01"):
            raise RiskInputError("risk_fraction_exceeds_hard_cap")

        if lev > lev_cap or lev > max_lev:
            raise RiskInputError("leverage_exceeds_hard_cap")

        if side == "LONG" and stop >= entry:
            raise RiskInputError("long_stop_must_be_below_entry")

        if side == "SHORT" and stop <= entry:
            raise RiskInputError("short_stop_must_be_above_entry")

        stop_distance = abs(entry - stop) * multiplier

        # Estimated round-trip fees and slippage per contract.
        entry_notional_per_contract = entry * multiplier
        round_trip_cost = (
            entry_notional_per_contract
            * Decimal("2")
            * (fee + slip)
        )

        estimated_loss_per_contract = (
            stop_distance + round_trip_cost
        )

        if estimated_loss_per_contract <= 0:
            raise RiskInputError("estimated_loss_invalid")

        risk_budget = wallet * risk
        margin_budget = wallet * margin_cap

        risk_limited_qty = (
            risk_budget / estimated_loss_per_contract
        )

        margin_per_contract = (
            entry_notional_per_contract / lev
        )

        margin_limited_qty = (
            margin_budget / margin_per_contract
        )

        raw_qty = min(
            risk_limited_qty,
            margin_limited_qty,
            max_qty,
        )

        quantity = floor_step(raw_qty, step)

        if quantity < min_qty:
            raise RiskInputError("quantity_below_exchange_minimum")

        notional = quantity * entry_notional_per_contract

        if notional < min_notional:
            raise RiskInputError("notional_below_exchange_minimum")

        estimated_risk = (
            quantity * estimated_loss_per_contract
        )

        estimated_margin = (
            notional / lev
        )

        if estimated_risk > risk_budget:
            raise RiskInputError("risk_budget_exceeded")

        if estimated_margin > margin_budget:
            raise RiskInputError("margin_budget_exceeded")

        return {
            "eligible": True,
            "blockers": [],
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "entry_price": str(entry),
            "stop_price": str(stop),
            "notional_usdt": str(notional),
            "leverage": str(lev),
            "risk_budget_usdt": str(risk_budget),
            "estimated_stop_loss_and_cost_usdt": str(estimated_risk),
            "estimated_margin_usdt": str(estimated_margin),
            "margin_utilization": str(estimated_margin / wallet),
            "orders_submitted": 0,
        }

    except RiskInputError as exc:
        return {
            "eligible": False,
            "blockers": [str(exc)],
            "orders_submitted": 0,
        }
    except Exception:
        return {
            "eligible": False,
            "blockers": ["unexpected_precheck_error"],
            "orders_submitted": 0,
        }
