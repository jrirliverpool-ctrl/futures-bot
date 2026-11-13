"""Strict Toobit USDT-M futures contract specifications.

Safety rules:
- LOT_SIZE quantities are underlying-token quantities.
- Order quantities are contract quantities.
- Contract quantity = token quantity / contractMultiplier.
- A zero or missing minimum notional is never guessed.
- Only the first risk tier is used for the tier-1 leverage ceiling.
"""

from datetime import datetime, timezone
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
)
import time

import requests


BASE_URL = "https://api.toobit.com"
EXCHANGE_INFO_PATH = "/api/v1/exchangeInfo"


def _finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if result != result or result in (float("inf"), float("-inf")):
        return None

    return result


def fetch_exchange_info(timeout=15, max_retries=3):
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise ValueError("max_retries must be an integer")
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    last_error = None

    for attempt in range(max_retries):
        try:
            response = requests.get(
                f"{BASE_URL}{EXCHANGE_INFO_PATH}",
                timeout=timeout,
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = max(0, int(retry_after))
                except (TypeError, ValueError):
                    wait_seconds = 2 ** attempt

                last_error = "HTTP 429 (rate limit)"
                if attempt < max_retries - 1:
                    time.sleep(wait_seconds)
                continue

            if 500 <= response.status_code < 600:
                last_error = f"HTTP {response.status_code}"
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                continue

            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("exchange_info_invalid_root")

            return payload

        except requests.exceptions.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

        except ValueError as exc:
            last_error = str(exc)
            break

    raise RuntimeError(
        f"exchangeInfo fetch failed after {max_retries} attempts: "
        f"{last_error}"
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
            f"duplicate_{filter_type}_filter_for_{contract.get('symbol', '<unknown>')}"
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
        raise ValueError(f"{field_name}={value!r} not finite for {symbol}")

    if number < 0:
        raise ValueError(f"{field_name}={value!r} negative for {symbol}")

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

    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name}_not_finite")

    return result


def _resolve_min_notional(contract, symbol):
    """Return (Decimal value, source description, verified bool).

    A positive official MIN_NOTIONAL.minNotional is preferred.
    A positive TRADE_AMOUNT.minAmount is the documented fallback.
    Zero/missing values remain unverified; no market-wide estimate is used.
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
            return primary, "MIN_NOTIONAL.minNotional", True

    if fallback_raw is not None:
        fallback = _decimal_value(
            fallback_raw,
            "TRADE_AMOUNT.minAmount",
            symbol,
        )

        if fallback > 0:
            return fallback, "TRADE_AMOUNT.minAmount", True

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

    return min_contract_qty, max_contract_qty, contract_step


def _resolve_tier1_max_leverage(contract, symbol):
    """Resolve tier-1 leverage without inventing a value.

    Preferred source: riskLimits[0].maxLeverage.
    Documented fallback: 1 / riskLimits[0].initialMargin.

    The fallback is explicitly labelled as derived from initial margin.
    It is not treated as a value returned directly by the exchange.
    """
    risk_limits = contract.get("riskLimits") or []

    if not isinstance(risk_limits, list):
        raise ValueError(f"riskLimits_invalid_for_{symbol}")

    if not risk_limits:
        return None, "unavailable", 0

    first_tier = risk_limits[0]

    if not isinstance(first_tier, dict):
        raise ValueError(f"riskLimits_tier1_invalid_for_{symbol}")

    raw_leverage = first_tier.get("maxLeverage")

    if raw_leverage is not None:
        leverage = _finite_float(raw_leverage)

        if leverage is not None and leverage > 0:
            return leverage, "riskLimits[0].maxLeverage", len(risk_limits)

    raw_initial_margin = first_tier.get("initialMargin")

    if raw_initial_margin is not None:
        initial_margin = _finite_float(raw_initial_margin)

        if (
            initial_margin is not None
            and initial_margin > 0
            and initial_margin <= 1
        ):
            derived = 1.0 / initial_margin

            if _finite_float(derived) is not None and derived > 0:
                return (
                    derived,
                    "derived_from_riskLimits[0].initialMargin",
                    len(risk_limits),
                )

    return None, "unavailable", len(risk_limits)


def parse_symbol_specs(contract):
    if not isinstance(contract, dict):
        raise ValueError("contract_row_invalid")

    symbol = contract.get("symbol", "<unknown>")

    lot_size = _extract_filter(contract, "LOT_SIZE")
    price_filter = _extract_filter(contract, "PRICE_FILTER")

    (
        min_notional,
        min_notional_source,
        min_notional_verified,
    ) = _resolve_min_notional(contract, symbol)

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

    (
        max_leverage_tier1,
        max_leverage_source,
        risk_limits_count,
    ) = _resolve_tier1_max_leverage(contract, symbol)

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

        # Compatibility fields used by existing sizing/execution modules.
        "min_qty": _to_float(min_contract_qty, "min_qty_contracts"),
        "max_qty": _to_float(max_contract_qty, "max_qty_contracts"),
        "qty_step": _to_float(contract_step, "qty_step_contracts"),

        # Explicit contract-unit fields.
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

        # Original LOT_SIZE values, in underlying-token units.
        "min_qty_token": _to_float(min_token_qty, "min_qty_token"),
        "max_qty_token": _to_float(max_token_qty, "max_qty_token"),
        "qty_step_token": _to_float(step_token_qty, "qty_step_token"),

        "min_notional": _to_float(min_notional, "min_notional"),
        "min_notional_source": min_notional_source,
        "min_notional_verified": min_notional_verified,

        "tick_size": _to_float(tick_size, "tick_size"),
        "min_price": _to_float(min_price, "min_price"),
        "max_price": _to_float(max_price, "max_price"),

        "max_leverage": max_leverage_tier1,
        "max_leverage_source": max_leverage_source,
        "risk_limits_count": risk_limits_count,

        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def parse_exchange_info(exchange_info, symbols=None):
    """Parse one exchangeInfo snapshot for one or more symbols.

    Returns a dict keyed by symbol. Each requested symbol must match exactly
    one contract row; missing or duplicate symbols fail closed.
    """
    if not isinstance(exchange_info, dict):
        raise ValueError("exchange_info_invalid_root")

    contracts = exchange_info.get("contracts")

    if not isinstance(contracts, list):
        raise ValueError("exchange_contracts_missing")

    if symbols is None:
        requested = [
            row.get("symbol")
            for row in contracts
            if isinstance(row, dict)
            and isinstance(row.get("symbol"), str)
        ]
    else:
        if not isinstance(symbols, (list, tuple, set)):
            raise ValueError("symbols_must_be_list_or_tuple_or_set")
        requested = list(symbols)

    if not requested:
        raise ValueError("no_symbols_requested")

    if any(not isinstance(symbol, str) or not symbol for symbol in requested):
        raise ValueError("requested_symbol_invalid")

    if len(requested) != len(set(requested)):
        raise ValueError("duplicate_requested_symbols")

    result = {}

    for symbol in requested:
        matches = [
            row for row in contracts
            if isinstance(row, dict)
            and row.get("symbol") == symbol
        ]

        if len(matches) != 1:
            raise ValueError(
                f"symbol_match_count_invalid:{symbol}:{len(matches)}"
            )

        result[symbol] = parse_symbol_specs(matches[0])

    return result


def get_symbol_specs_from_info(exchange_info, symbol):
    parsed = parse_exchange_info(exchange_info, [symbol])
    return parsed[symbol]


def get_symbol_specs(symbol="BTC-SWAP-USDT"):
    exchange_info = fetch_exchange_info()
    return get_symbol_specs_from_info(exchange_info, symbol)


def get_symbols_specs(symbols):
    """Fetch exchangeInfo once and parse all requested symbols."""
    exchange_info = fetch_exchange_info()
    return parse_exchange_info(exchange_info, symbols)


def validate_specs(specs):
    """Return (errors, warnings) for parsed contract specifications."""
    errors = []
    warnings = []

    if not isinstance(specs, dict):
        return ["specs must be dict"], warnings

    if specs.get("status") != "TRADING":
        errors.append(
            f"status must be TRADING (got {specs.get('status')!r})"
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
            or not _finite_float(value)
            or value <= 0
        ):
            errors.append(f"{key} must be finite and > 0")

    min_notional = specs.get("min_notional")

    if (
        isinstance(min_notional, bool)
        or not isinstance(min_notional, (int, float))
        or _finite_float(min_notional) is None
        or min_notional < 0
    ):
        errors.append("min_notional must be finite and non-negative")

    if (
        isinstance(specs.get("min_qty"), (int, float))
        and isinstance(specs.get("max_qty"), (int, float))
        and specs["min_qty"] > specs["max_qty"]
    ):
        errors.append("min_qty exceeds max_qty")

    if specs.get("min_notional_verified") is not True:
        warnings.append(
            "No positive exchange minimum notional was verified; "
            "new entries must remain blocked."
        )
    elif min_notional <= 0:
        errors.append(
            "verified minimum notional must be positive"
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
            "max_leverage unavailable; leverage constraint cannot be enforced"
        )
    elif (
        isinstance(max_leverage, bool)
        or not isinstance(max_leverage, (int, float))
        or _finite_float(max_leverage) is None
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