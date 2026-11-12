
def _parse_unverified_specs(raw_contract, symbol):
    """Parse all contract fields while keeping minimum notional unknown."""

    if not isinstance(raw_contract, dict):
        raise ValueError("contract_row_invalid")

    filters = raw_contract.get("filters")

    if not isinstance(filters, list):
        raise ValueError("contract_filters_missing")

    copied = dict(raw_contract)

    copied_filters = [
        item
        for item in filters
        if not (
            isinstance(item, dict)
            and item.get("filterType") in (
                "MIN_NOTIONAL",
                "TRADE_AMOUNT",
            )
        )
    ]

    copied_filters.append({
        "filterType": "MIN_NOTIONAL",
        "minNotional": "0",
    })

    copied["filters"] = copied_filters

    specs = parse_symbol_specs(copied)

    if specs.get("symbol") != symbol:
        raise ValueError("contract_symbol_mismatch")

    return specs


def resolve_specs(raw_contract, symbol, price=None):
    """Resolve verified exchange minimum or explicitly marked estimate."""

    try:
        specs = parse_symbol_specs(raw_contract)

    except ValueError as original_error:
        message = str(original_error)

        fallback_errors = (
            "minimum_order_value_missing_or_zero",
            "minimum_order_value_filter_missing",
            "minimum_order_value_zero",
        )

        if not any(
            marker in message
            for marker in fallback_errors
        ):
            raise

        specs = _parse_unverified_specs(
            raw_contract,
            symbol,
        )

    if specs.get("symbol") != symbol:
        raise ValueError("contract_symbol_mismatch")

    if specs.get("min_notional_verified") is True:
        official_value = dec(
            specs.get("min_notional"),
            f"{symbol}_min_notional",
        )

        if official_value <= 0:
            raise ValueError("verified_minimum_must_be_positive")

        specs["min_notional_is_estimate"] = False
        return specs

    if price is None:
        raise ValueError(
            f"{symbol}_price_required_for_estimated_minimum"
        )

    price = dec(
        price,
        f"{symbol}_ticker_price",
    )

    quantities = minimum_order_quantities(
        raw_contract,
        specs,
        symbol,
    )

    # Use rounded valid token quantity, not raw LOT_SIZE.minQty.
    estimated_notional = (
        quantities["rounded_token_qty"] * price
    )

    if estimated_notional <= 0:
        raise ValueError(
            f"{symbol}_estimated_minimum_invalid"
        )

    specs["min_notional"] = float(estimated_notional)

    specs["min_notional_verified"] = False
    specs["min_notional_is_estimate"] = True

    specs["min_notional_source"] = (
        "ESTIMATE:rounded_LOT_SIZE_token_qty"
        "*public_ticker_price"
    )

    return specs
