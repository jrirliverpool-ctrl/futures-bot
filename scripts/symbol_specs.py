
"""Strict Toobit USDT-M futures contract specifications.

v3.10.0

Important units:
- LOT_SIZE minQty/maxQty/stepSize are underlying-token quantities.
- OrderRequest.qty and Toobit V2 order quantity are contract quantities.
- Contract quantity = underlying-token quantity / contractMultiplier.

A zero MIN_NOTIONAL is preserved as zero and marked unverified.
No estimated or invented exchange minimum is substituted here.
"""

import time
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
)
from datetime import datetime, timezone

import requests


BASE_URL = "https://api.toobit.com"
EXCHANGE_INFO_PATH = "/api/v1/exchangeInfo"


def fetch_exchange_info(timeout=15, max_retries=3):
    last_err = None

    for attempt in range(max_retries):
        try:
            response = requests.get(
                f"{BASE_URL}{EXCHANGE_INFO_PATH}",
                timeout=timeout,
            )

            if response.status_code == 429:
                wait = int(
                    response.headers.get("Retry-After", 2 ** attempt)
                )
                time.sleep(wait)
                last_err = "HTTP 429 (rate limit)"
                continue

            if 500 <= response.status_code < 600:
                last_err = f"HTTP {response.status_code}"

                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

                continue

            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("exchange_info_invalid_root")

            return payload

        except requests.exceptions.RequestException as exc:
            last_err = exc

            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

        except ValueError as exc:
            last_err = exc
            break

    raise RuntimeError(
        f"exchangeInfo fetch failed after {max_retries} attempts: "
        f"{last_err}"
    )


def _extract_filter(contract, filter_type):
    filters = contract.get("filters")

    if not isinstance(filters, list):
        raise ValueError(
            f"filters_missing_or_invalid_for_"
            f"{contract.get('symbol', '<unknown>')}"
        )

    matches = [
        item
        for item in filters
        if isinstance(item, dict)
        and item.get("filterType") == filter_type
    ]

    if len(matches) > 1:
        raise ValueError(
            f"duplicate_{filter_type}_filter_for_"
            f"{contract.get('symbol', '<unknown>')}"
        )

    return matches[0] if matches else {}


def _decimal_value(value, field_name, symbol):
    if value is None or isinstance(value, bool):
        raise ValueError(
            f"{field_name} missing or invalid for {symbol}"
        )

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(
            f"{field_name}={value!r} not numeric for {symbol}"
        ) from None

    if not number.is_finite():
        raise ValueError(
            f"{field_name}={value!r} not finite for {symbol}"
        )

    if number < 0:
        raise ValueError(
            f"{field_name}={value!r} negative for {symbol}"
        )

    return number


def _positive_decimal(value, field_name, symbol):
    number = _decimal_value(value, field_name, symbol)

    if number <= 0:
        raise ValueError(
            f"{field_name}={value!r} must be positive for {symbol}"
        )

    return number


def _to_float(value, field_name):
    result = float(value)

    if result != result or result in (
        float("inf"),
        float("-inf"),
    ):
        raise ValueError(f"{field_name}_not_finite")

    return result


def _resolve_min_notional(contract, symbol):
    """Return (value, source, verified).

    Positive MIN_NOTIONAL has priority.
    Positive TRADE_AMOUNT.minAmount is the documented fallback.

    A zero MIN_NOTIONAL with no fallback is retained as zero.
    It is not converted into an estimated exchange rule.
    """

    min_filter = _extract_filter(contract, "MIN_NOTIONAL")
    trade_filter = _extract_filter(contract, "TRADE_AMOUNT")

    primary_raw = min_filter.get("minNotional")
    fallback_raw = trade_filter.get("minAmount")

    if primary_raw is not None:
        primary = _decimal_value(
            primary_raw,
            "MIN_NOTIONAL.minNotional",
            symbol,
        )

        if primary > 0:
            return (
                primary,
                "MIN_NOTIONAL.minNotional",
                True,
            )

    if fallback_raw is not None:
        fallback = _decimal_value(
            fallback_raw,
            "TRADE_AMOUNT.minAmount",
            symbol,
        )

        if fallback > 0:
            return (
                fallback,
                "TRADE_AMOUNT.minAmount",
                True,
            )

        raise ValueError(
            f"{symbol}_minimum_order_value_zero:"
            f"MIN_NOTIONAL.minNotional={primary_raw!r};"
            f"TRADE_AMOUNT.minAmount={fallback_raw!r}"
        )

    if primary_raw is not None:
        return (
            Decimal("0"),
            "MIN_NOTIONAL.minNotional=0; no positive fallback",
            False,
        )

    raise ValueError(
        f"{symbol}_minimum_order_value_filter_missing:"
        f"MIN_NOTIONAL.minNotional={primary_raw!r};"
        f"TRADE_AMOUNT.minAmount={fallback_raw!r}"
    )


def _convert_lot_quantities_to_contracts(
    min_token_qty,
    max_token_qty,
    step_token_qty,
    multiplier,
    symbol,
):
    """Convert exchange token quantities into order contract quantities."""

    min_contract_raw = min_token_qty / multiplier
    max_contract_raw = max_token_qty / multiplier
    contract_step = step_token_qty / multiplier

    if contract_step <= 0:
        raise ValueError(f"{symbol}_contract_step_invalid")

    min_steps = (
        min_contract_raw / contract_step
    ).to_integral_value(rounding=ROUND_CEILING)

    max_steps = (
        max_contract_raw / contract_step
    ).to_integral_value(rounding=ROUND_FLOOR)

    min_contract_qty = min_steps * contract_step
    max_contract_qty = max_steps * contract_step

    if min_contract_qty <= 0:
        raise ValueError(f"{symbol}_minimum_contract_qty_invalid")

    if max_contract_qty <= 0:
        raise ValueError(f"{symbol}_maximum_contract_qty_invalid")

    if min_contract_qty > max_contract_qty:
        raise ValueError(f"{symbol}_contract_quantity_range_invalid")

    return (
        min_contract_qty,
        max_contract_qty,
        contract_step,
    )


def parse_symbol_specs(contract):
    if not isinstance(contract, dict):
        raise ValueError("contract_row_invalid")

    symbol = contract.get("symbol", "<unknown>")

    lot_size = _extract_filter(contract, "LOT_SIZE")
    price_filter = _extract_filter(contract, "PRICE_FILTER")

    min_notional, min_notional_source, min_notional_verified = (
        _resolve_min_notional(contract, symbol)
    )

    multiplier = _positive_decimal(
        contract.get("contractMultiplier"),
        "contractMultiplier",
        symbol,
    )

    min_token_qty = _positive_decimal(
        lot_size.get("minQty"),
        "LOT_SIZE.minQty",
        symbol,
    )

    max_token_qty = _positive_decimal(
        lot_size.get("maxQty"),
        "LOT_SIZE.maxQty",
        symbol,
    )

    step_token_qty = _positive_decimal(
        lot_size.get("stepSize"),
        "LOT_SIZE.stepSize",
        symbol,
    )

    if max_token_qty < min_token_qty:
        raise ValueError(f"{symbol}_token_quantity_range_invalid")

    (
        min_contract_qty,
        max_contract_qty,
        contract_step,
    ) = _convert_lot_quantities_to_contracts(
        min_token_qty,
        max_token_qty,
        step_token_qty,
        multiplier,
        symbol,
    )

    tick_size = _positive_decimal(
        price_filter.get("tickSize"),
        "PRICE_FILTER.tickSize",
        symbol,
    )

    min_price = _decimal_value(
        price_filter.get("minPrice", "0"),
        "PRICE_FILTER.minPrice",
        symbol,
    )

    max_price = _decimal_value(
        price_filter.get("maxPrice", "0"),
        "PRICE_FILTER.maxPrice",
        symbol,
    )

    risk_limits = contract.get("riskLimits") or []

    if not isinstance(risk_limits, list):
        raise ValueError(f"riskLimits_invalid_for_{symbol}")

    max_leverage_tier1 = None

    if risk_limits and isinstance(risk_limits[0], dict):
        raw_leverage = risk_limits[0].get("maxLeverage")

        if raw_leverage is not None:
            try:
                value = float(raw_leverage)

                if (
                    value > 0
                    and value == value
                    and value not in (
                        float("inf"),
                        float("-inf"),
                    )
                ):
                    max_leverage_tier1 = value

            except (TypeError, ValueError, OverflowError):
                pass

    return {
        "symbol": symbol,
        "status": contract.get("status"),
        "base_asset": contract.get("baseAsset"),
        "quote_asset": contract.get("quoteAsset"),
        "margin_token": contract.get("marginToken"),

        "quantity_unit": "contracts",

        "contract_multiplier": _to_float(
            multiplier,
            "contract_multiplier",
        ),

        # Compatibility keys used by the existing sizing and execution
        # code. These are now explicitly in CONTRACT units.
        "min_qty": _to_float(
            min_contract_qty,
            "min_qty_contracts",
        ),
        "max_qty": _to_float(
            max_contract_qty,
            "max_qty_contracts",
        ),
        "qty_step": _to_float(
            contract_step,
            "qty_step_contracts",
        ),

        # Explicit unit-labelled fields.
        "min_contract_qty": _to_float(
            min_contract_qty,
            "min_contract_qty",
        ),
        "max_contract_qty": _to_float(
            max_contract_qty,
            "max_contract_qty",
        ),
        "contract_qty_step": _to_float(
            contract_step,
            "contract_qty_step",
        ),

        # Original exchange values, preserved in underlying-token units.
        "min_qty_token": _to_float(
            min_token_qty,
            "min_qty_token",
        ),
        "max_qty_token": _to_float(
            max_token_qty,
            "max_qty_token",
        ),
        "qty_step_token": _to_float(
            step_token_qty,
            "qty_step_token",
        ),

        "min_notional": _to_float(
            min_notional,
            "min_notional",
        ),
        "min_notional_source": min_notional_source,
        "min_notional_verified": min_notional_verified,

        "tick_size": _to_float(tick_size, "tick_size"),
        "min_price": _to_float(min_price, "min_price"),
        "max_price": _to_float(max_price, "max_price"),

        "max_leverage": max_leverage_tier1,
        "risk_limits_count": len(risk_limits),

        "fetched_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def get_symbol_specs(symbol="BTC-SWAP-USDT"):
    info = fetch_exchange_info()

    contracts = info.get("contracts")

    if not isinstance(contracts, list):
        raise ValueError("exchange_contracts_missing")

    matches = [
        row
        for row in contracts
        if isinstance(row, dict)
        and row.get("symbol") == symbol
    ]

    if len(matches) != 1:
        raise ValueError(
            f"symbol_match_count_invalid:{symbol}:{len(matches)}"
        )

    return parse_symbol_specs(matches[0])


def validate_specs(specs):
    """Return (errors, warnings) for parsed contract specifications."""

    errors = []
    warnings = []

    if not isinstance(specs, dict):
        return ["specs must be dict"], warnings

    if specs.get("status") != "TRADING":
        errors.append(
            f"status must be TRADING "
            f"(got {specs.get('status')!r})"
        )

    if specs.get("quantity_unit") != "contracts":
        errors.append("quantity_unit must be contracts")

    positive_fields = (
        "contract_multiplier",
        "min_qty",
        "max_qty",
        "qty_step",
        "tick_size",
        "min_qty_token",
        "max_qty_token",
        "qty_step_token",
    )

    for key in positive_fields:
        value = specs.get(key)

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            errors.append(f"{key} must be numeric")
            continue

        if (
            value != value
            or value in (float("inf"), float("-inf"))
            or value <= 0
        ):
            errors.append(f"{key} must be finite and > 0")

    if (
        isinstance(specs.get("min_notional"), bool)
        or not isinstance(specs.get("min_notional"), (int, float))
        or specs.get("min_notional") < 0
    ):
        errors.append("min_notional must be finite and non-negative")

    if (
        isinstance(specs.get("min_qty"), (int, float))
        and isinstance(specs.get("max_qty"), (int, float))
        and specs["min_qty"] > specs["max_qty"]
    ):
        errors.append("min_qty exceeds max_qty")

    if not specs.get("min_notional_verified", False):
        warnings.append(
            "No positive exchange minimum notional was verified; "
            "do not infer one from this field."
        )

    for key in (
        "symbol",
        "quote_asset",
        "margin_token",
        "min_notional_source",
    ):
        if not isinstance(specs.get(key), str) or not specs[key]:
            errors.append(f"{key} must be a non-empty string")

    max_leverage = specs.get("max_leverage")

    if max_leverage is None:
        errors.append(
            "max_leverage not available — "
            "leverage constraint cannot be enforced"
        )

    elif (
        isinstance(max_leverage, bool)
        or not isinstance(max_leverage, (int, float))
        or max_leverage <= 0
    ):
        errors.append(f"max_leverage invalid ({max_leverage!r})")

    elif max_leverage < 3:
        warnings.append(
            f"max_leverage={max_leverage} < strategy target 3x"
        )

    return errors, warnings


def print_specs(specs):
    print("\n" + "=" * 60)
    print(f"SYMBOL SPECS — {specs.get('symbol')}")
    print("=" * 60)

    for key, value in specs.items():
        print(f"  {key}: {value}")

    print("=" * 60)


if __name__ == "__main__":
    print("Fetching BTC-SWAP-USDT specs...\n")

    try:
        specs = get_symbol_specs("BTC-SWAP-USDT")

    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    print_specs(specs)

    errors, warnings = validate_specs(specs)

    for warning in warnings:
        print(f"WARNING: {warning}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")

        print("\nVALIDATION: FAILED")
        raise SystemExit(1)

    print("All critical quantity fields valid.")
    print("VALIDATION: PASSED")
