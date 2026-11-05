
"""Strict Toobit USDT-M futures contract specifications.

v3.9.4
- Reads MIN_NOTIONAL.minNotional as the preferred minimum.
- If MIN_NOTIONAL is exactly zero, checks TRADE_AMOUNT.minAmount.
- Uses only a verified positive minimum value.
- Rejects missing, malformed, negative, non-finite or ambiguous filters.
- Never invents contract specifications.
"""

import time
from decimal import Decimal, InvalidOperation
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
            return response.json()

        except requests.exceptions.RequestException as exc:
            last_err = exc

            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"exchangeInfo fetch failed after {max_retries} attempts: "
        f"{last_err}"
    )


def _extract_filter(contract, filter_type):
    filters = contract.get("filters")

    if not isinstance(filters, list):
        raise ValueError(
            f"filters_missing_or_invalid_for_{contract.get('symbol', '<unknown>')}"
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
        raise ValueError(f"{field_name} missing or invalid for {symbol}")

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


def _safe_positive_float(value, field_name, symbol):
    number = _decimal_value(value, field_name, symbol)

    if number <= 0:
        raise ValueError(
            f"{field_name}={value!r} must be positive for {symbol}"
        )

    return float(number)


def _safe_non_negative_float(
    value,
    field_name,
    symbol,
    default=0.0,
):
    if value is None:
        return default

    number = _decimal_value(value, field_name, symbol)
    return float(number)


def _resolve_min_notional(contract, symbol):
    """
    Resolve the minimum futures transaction value.

    Priority:
      1. Positive MIN_NOTIONAL.minNotional
      2. Positive TRADE_AMOUNT.minAmount

    A zero MIN_NOTIONAL value is treated as unavailable for this
    calculation, so the documented TRADE_AMOUNT field is checked.

    A malformed or negative primary value is rejected.
    If neither field supplies a positive value, the contract is rejected.
    """

    min_notional_filter = _extract_filter(
        contract,
        "MIN_NOTIONAL",
    )

    trade_amount_filter = _extract_filter(
        contract,
        "TRADE_AMOUNT",
    )

    primary_raw = min_notional_filter.get("minNotional")
    fallback_raw = trade_amount_filter.get("minAmount")

    if primary_raw is not None:
        primary = _decimal_value(
            primary_raw,
            "MIN_NOTIONAL.minNotional",
            symbol,
        )

        if primary > 0:
            return (
                float(primary),
                "MIN_NOTIONAL.minNotional",
            )

        # A zero primary value is not used as an order minimum.
        # Check the documented alternative field instead.

    if fallback_raw is not None:
        fallback = _decimal_value(
            fallback_raw,
            "TRADE_AMOUNT.minAmount",
            symbol,
        )

        if fallback > 0:
            return (
                float(fallback),
                "TRADE_AMOUNT.minAmount",
            )

        raise ValueError(
            f"{symbol}_minimum_order_value_zero:"
            f"MIN_NOTIONAL.minNotional={primary_raw!r};"
            f"TRADE_AMOUNT.minAmount={fallback_raw!r}"
        )

    raise ValueError(
        f"{symbol}_minimum_order_value_missing_or_zero:"
        f"MIN_NOTIONAL.minNotional={primary_raw!r};"
        f"TRADE_AMOUNT.minAmount={fallback_raw!r}"
    )


def parse_symbol_specs(contract):
    if not isinstance(contract, dict):
        raise ValueError("contract_row_invalid")

    symbol = contract.get("symbol", "<unknown>")

    lot_size = _extract_filter(
        contract,
        "LOT_SIZE",
    )

    price_filter = _extract_filter(
        contract,
        "PRICE_FILTER",
    )

    min_notional, min_notional_source = _resolve_min_notional(
        contract,
        symbol,
    )

    contract_multiplier = _safe_positive_float(
        contract.get("contractMultiplier"),
        "contractMultiplier",
        symbol,
    )

    risk_limits = contract.get("riskLimits") or []

    if not isinstance(risk_limits, list):
        raise ValueError(
            f"riskLimits_invalid_for_{symbol}"
        )

    max_leverage_tier1 = None

    if risk_limits and isinstance(risk_limits[0], dict):
        raw_leverage = risk_limits[0].get("maxLeverage")

        if raw_leverage is not None:
            try:
                leverage_value = float(raw_leverage)

                if (
                    leverage_value == leverage_value
                    and leverage_value not in (
                        float("inf"),
                        float("-inf"),
                    )
                    and leverage_value > 0
                ):
                    max_leverage_tier1 = leverage_value

            except (ValueError, TypeError):
                pass

    return {
        "symbol": symbol,
        "status": contract.get("status"),
        "base_asset": contract.get("baseAsset"),
        "quote_asset": contract.get("quoteAsset"),
        "margin_token": contract.get("marginToken"),

        "contract_multiplier": contract_multiplier,

        "min_qty": _safe_non_negative_float(
            lot_size.get("minQty"),
            "minQty",
            symbol,
        ),

        "max_qty": _safe_non_negative_float(
            lot_size.get("maxQty"),
            "maxQty",
            symbol,
        ),

        "qty_step": _safe_non_negative_float(
            lot_size.get("stepSize"),
            "stepSize",
            symbol,
        ),

        "min_notional": min_notional,
        "min_notional_source": min_notional_source,

        "tick_size": _safe_positive_float(
            price_filter.get("tickSize"),
            "tickSize",
            symbol,
        ),

        "min_price": _safe_non_negative_float(
            price_filter.get("minPrice"),
            "minPrice",
            symbol,
        ),

        "max_price": _safe_non_negative_float(
            price_filter.get("maxPrice"),
            "maxPrice",
            symbol,
        ),

        "max_leverage": max_leverage_tier1,
        "risk_limits_count": len(risk_limits),

        "fetched_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def get_symbol_specs(symbol="BTC-SWAP-USDT"):
    info = fetch_exchange_info()

    if not isinstance(info, dict):
        raise ValueError("exchange_info_invalid")

    contracts = info.get("contracts", [])

    if not isinstance(contracts, list):
        raise ValueError("exchange_contracts_missing")

    for contract in contracts:
        if (
            isinstance(contract, dict)
            and contract.get("symbol") == symbol
        ):
            return parse_symbol_specs(contract)

    raise ValueError(
        f"Symbol '{symbol}' not found."
    )


def validate_specs(specs):
    """Return (errors, warnings) for parsed contract specifications."""

    errors = []
    warnings = []

    if not isinstance(specs, dict):
        errors.append("specs must be dict")
        return errors, warnings

    if specs.get("status") != "TRADING":
        errors.append(
            f"status must be TRADING "
            f"(got {specs.get('status')!r})"
        )

    positive_fields = (
        "contract_multiplier",
        "min_qty",
        "max_qty",
        "qty_step",
        "tick_size",
        "min_notional",
    )

    for key in positive_fields:
        value = specs.get(key)

        if (
            value is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            errors.append(
                f"{key} must be numeric "
                f"(got {type(value).__name__}={value!r})"
            )
            continue

        if (
            value != value
            or value in (float("inf"), float("-inf"))
        ):
            errors.append(
                f"{key} not finite ({value})"
            )
            continue

        if value <= 0:
            errors.append(
                f"{key} must be > 0 (got {value})"
            )

    for key in (
        "symbol",
        "quote_asset",
        "margin_token",
        "min_notional_source",
    ):
        if (
            not isinstance(specs.get(key), str)
            or not specs[key]
        ):
            errors.append(
                f"{key} must be non-empty string"
            )

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
        errors.append(
            f"max_leverage invalid ({max_leverage!r})"
        )

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
        specs = get_symbol_specs(
            "BTC-SWAP-USDT"
        )

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

    print("All critical fields valid.")
    print("VALIDATION: PASSED")
