
"""Read-only Toobit USDT-M capital feasibility diagnostic.

Uses official positive minimum filters when available.
If those filters are zero/missing, estimates minimum notional from
LOT_SIZE.minQty * current public contract ticker price.

An estimated minimum is NOT an exchange-confirmed order minimum.
This module never submits, amends, or cancels orders.
LIVE execution remains disabled.
"""

from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING

import requests

sys.path.insert(0, os.path.dirname(__file__))

from symbol_specs import fetch_exchange_info, parse_symbol_specs
from toobit_readonly_preflight import build_snapshot, PreflightError


BASE_URL = "https://api.toobit.com"
TICKER_URL = f"{BASE_URL}/quote/v1/contract/ticker/price"

STRATEGY_LEVERAGE_CAP = Decimal("3")
MARGIN_UTILIZATION_CAP = Decimal("0.50")
RISK_PER_TRADE = Decimal("0.001")

TIMEOUT = 20
MAX_RETRIES = 3


class CapitalReportError(RuntimeError):
    pass


def dec(value, field, allow_zero=False):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}_missing_or_invalid")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field}_not_numeric") from None

    if not result.is_finite():
        raise ValueError(f"{field}_not_finite")

    if result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"{field}_out_of_range")

    return result


def get_public_json(url):
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=TIMEOUT)

            if response.status_code == 429:
                delay = int(
                    response.headers.get("Retry-After", 2 ** attempt)
                )
                time.sleep(delay)
                last_error = "HTTP_429"
                continue

            if response.status_code >= 500:
                last_error = f"HTTP_{response.status_code}"
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                continue

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__}:{str(exc)[:120]}"
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"public_ticker_fetch_failed:{last_error}")


def fetch_ticker_prices():
    """Fetch public ticker prices. No account credentials are used."""
    payload = get_public_json(TICKER_URL)

    if isinstance(payload, dict):
        payload = payload.get("data", payload)

    if not isinstance(payload, list):
        raise ValueError("ticker_response_not_array")

    prices = {}

    for row in payload:
        if not isinstance(row, dict):
            continue

        symbol = row.get("s") or row.get("symbol")
        price = row.get("p") or row.get("price")

        if not isinstance(symbol, str) or price is None:
            continue

        try:
            prices[symbol] = dec(price, f"{symbol}_ticker_price")
        except ValueError:
            continue

    if not prices:
        raise ValueError("ticker_prices_empty")

    return prices


def contract_filter(contract, filter_type):
    filters = contract.get("filters")

    if not isinstance(filters, list):
        raise ValueError("contract_filters_missing")

    matches = [
        item for item in filters
        if isinstance(item, dict)
        and item.get("filterType") == filter_type
    ]

    if len(matches) > 1:
        raise ValueError(f"duplicate_filter_{filter_type}")

    return matches[0] if matches else {}


def contract_map(exchange_info):
    if not isinstance(exchange_info, dict):
        raise CapitalReportError("exchange_info_invalid")

    rows = exchange_info.get("contracts")

    if not isinstance(rows, list):
        raise CapitalReportError("exchange_contracts_missing")

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            raise CapitalReportError("exchange_contract_row_invalid")

        symbol = row.get("symbol")

        if not isinstance(symbol, str) or not symbol:
            raise CapitalReportError("exchange_contract_symbol_invalid")

        if symbol in result:
            raise CapitalReportError("duplicate_exchange_contract")

        result[symbol] = row

    return result


def leverage_map(rows, symbols):
    if not isinstance(rows, list):
        raise CapitalReportError("leverage_snapshot_invalid")

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            raise CapitalReportError("leverage_row_invalid")

        symbol = row.get("symbolId")

        if symbol not in symbols:
            continue

        if symbol in result:
            raise CapitalReportError("duplicate_account_leverage")

        result[symbol] = dec(
            row.get("leverage"),
            f"{symbol}_account_leverage",
        )

    if set(symbols) - set(result):
        raise CapitalReportError("account_leverage_coverage_incomplete")

    return result


def contract_max_leverage(raw_contract, specs, symbol):
    direct = specs.get("max_leverage")

    if direct is not None:
        return dec(direct, f"{symbol}_contract_max_leverage")

    tiers = raw_contract.get("riskLimits")

    if not isinstance(tiers, list) or not tiers:
        raise ValueError(f"{symbol}_risk_limits_missing")

    first = tiers[0]

    if not isinstance(first, dict):
        raise ValueError(f"{symbol}_risk_tier_invalid")

    initial_margin = dec(
        first.get("initialMargin"),
        f"{symbol}_initial_margin",
    )

    if initial_margin > 1:
        raise ValueError(f"{symbol}_initial_margin_out_of_range")

    return Decimal("1") / initial_margin


def resolve_specs(raw_contract, symbol, price):
    """Resolve official minimum first, then a clearly labelled estimate."""

    try:
        specs = parse_symbol_specs(raw_contract)
        specs["min_notional_is_estimate"] = False
        return specs

    except ValueError as original_error:
        message = str(original_error)

        # Only use the price-based estimate for the known missing/zero
        # minimum-filter condition. Do not bypass unrelated validation.
        allowed_fallback = (
            "minimum_order_value_missing_or_zero" in message
            or "minimum_order_value_zero" in message
        )

        if not allowed_fallback:
            raise

        lot = contract_filter(raw_contract, "LOT_SIZE")

        min_qty = dec(
            lot.get("minQty"),
            f"{symbol}_LOT_SIZE_minQty",
        )

        estimated_notional = min_qty * price

        if estimated_notional <= 0:
            raise ValueError(f"{symbol}_estimated_minimum_invalid")

        # Inject only into a copy. The original exchange response is untouched.
        copied = dict(raw_contract)
        copied_filters = []

        for item in raw_contract.get("filters", []):
            if not isinstance(item, dict):
                copied_filters.append(item)
                continue

            if item.get("filterType") == "MIN_NOTIONAL":
                copied_filters.append({
                    **item,
                    "minNotional": str(estimated_notional),
                })
            else:
                copied_filters.append(item)

        if not any(
            isinstance(item, dict)
            and item.get("filterType") == "MIN_NOTIONAL"
            for item in copied_filters
        ):
            copied_filters.append({
                "filterType": "MIN_NOTIONAL",
                "minNotional": str(estimated_notional),
            })

        copied["filters"] = copied_filters

        specs = parse_symbol_specs(copied)
        specs["min_notional_is_estimate"] = True
        specs["min_notional_source"] = (
            "ESTIMATE:LOT_SIZE.minQty*public_ticker_price"
        )

        return specs


def minimum_order_quantities(raw_contract, specs, symbol):
    """Convert LOT_SIZE token quantities to contract quantities."""

    lot = contract_filter(raw_contract, "LOT_SIZE")

    multiplier = dec(
        raw_contract.get("contractMultiplier"),
        f"{symbol}_contract_multiplier",
    )

    min_token_qty = dec(
        lot.get("minQty"),
        f"{symbol}_min_token_qty",
    )

    step_token_qty = dec(
        lot.get("stepSize"),
        f"{symbol}_step_token_qty",
    )

    min_contracts_raw = min_token_qty / multiplier
    contract_step = step_token_qty / multiplier

    if contract_step <= 0:
        raise ValueError(f"{symbol}_contract_step_invalid")

    # Round UP to a valid exchange step. Never round below minimum.
    steps = (
        min_contracts_raw / contract_step
    ).to_integral_value(rounding=ROUND_CEILING)

    min_contracts = steps * contract_step
    rounded_token_qty = min_contracts * multiplier

    return {
        "min_token_qty": min_token_qty,
        "step_token_qty": step_token_qty,
        "min_contract_qty": min_contracts,
        "contract_step": contract_step,
        "rounded_token_qty": rounded_token_qty,
    }


def evaluate_capital_feasibility(
    snapshot,
    exchange_info,
    symbols,
    *,
    prices=None,
    strategy_leverage_cap="3",
    margin_utilization_cap="0.50",
    risk_per_trade="0.001",
):
    """Read-only minimum-order margin assessment. Never approves a trade."""

    if not isinstance(snapshot, dict):
        raise CapitalReportError("account_snapshot_invalid")

    if snapshot.get("read_only") is not True:
        raise CapitalReportError("account_snapshot_not_read_only")

    if not isinstance(symbols, list) or not symbols:
        raise CapitalReportError("symbols_invalid")

    if not all(isinstance(item, str) and item for item in symbols):
        raise CapitalReportError("symbol_name_invalid")

    if len(symbols) != len(set(symbols)):
        raise CapitalReportError("duplicate_configured_symbols")

    wallet = snapshot.get("wallet")

    if not isinstance(wallet, dict) or wallet.get("coin") != "USDT":
        raise CapitalReportError("usdt_wallet_missing")

    available = dec(
        wallet.get("availableBalance"),
        "available_usdt",
        allow_zero=True,
    )

    leverage_cap = dec(strategy_leverage_cap, "strategy_leverage_cap")
    margin_cap = dec(margin_utilization_cap, "margin_utilization_cap")
    risk_fraction = dec(risk_per_trade, "risk_per_trade")

    if leverage_cap > 3:
        raise CapitalReportError("strategy_leverage_cap_exceeds_hard_cap")

    if margin_cap > Decimal("0.50"):
        raise CapitalReportError("margin_utilization_cap_exceeds_hard_cap")

    if margin_cap > 1:
        raise CapitalReportError("margin_utilization_cap_invalid")

    if risk_fraction > Decimal("0.01"):
        raise CapitalReportError("risk_fraction_exceeds_hard_cap")

    prices = prices or {}
    contracts = contract_map(exchange_info)
    account_leverage = leverage_map(
        snapshot.get("leverage_settings"),
        symbols,
    )

    margin_budget = available * margin_cap
    reports = []

    for symbol in symbols:
        raw = contracts.get(symbol)

        if raw is None:
            reports.append({
                "symbol": symbol,
                "capital_only_feasible": False,
                "trading_eligible": False,
                "blockers": ["contract_not_found"],
            })
            continue

        blockers = []

        try:
            price = dec(prices.get(symbol), f"{symbol}_ticker_price")
            specs = resolve_specs(raw, symbol, price)

            if specs.get("symbol") != symbol:
                raise ValueError("contract_symbol_mismatch")

            if specs.get("status") != "TRADING":
                blockers.append("contract_not_trading")

            quantities = minimum_order_quantities(raw, specs, symbol)

            min_notional = dec(
                specs.get("min_notional"),
                f"{symbol}_min_notional",
            )

            # For estimated minimums, recompute notional using the rounded
            # valid contract quantity rather than the unrounded token minimum.
            if specs.get("min_notional_is_estimate"):
                min_notional = (
                    quantities["rounded_token_qty"] * price
                )

            contract_lev = contract_max_leverage(raw, specs, symbol)
            account_lev = account_leverage[symbol]

            allowed_lev = min(
                leverage_cap,
                contract_lev,
                account_lev,
            )

            if allowed_lev <= 0:
                blockers.append("effective_leverage_invalid")

            minimum_margin = None
            margin_fits = False

            if not blockers:
                minimum_margin = min_notional / allowed_lev

                margin_fits = (
                    available > 0
                    and minimum_margin <= margin_budget
                )

                if not margin_fits:
                    blockers.append("minimum_order_margin_exceeds_budget")

            # These are mandatory before any real trade can be considered.
            blockers.extend([
                "verified_user_fee_rate_required",
                "entry_and_stop_prices_required",
                "stop_loss_risk_budget_check_required",
                "partial_fill_and_timeout_recovery_required",
                "fill_ledger_and_position_reconciliation_required",
                "exchange_protection_integration_required",
            ])

            capital_only = (
                margin_fits
                and specs.get("status") == "TRADING"
                and "effective_leverage_invalid" not in blockers
            )

            reports.append({
                "symbol": symbol,
                "contract_status": specs.get("status"),
                "ticker_price_usdt": str(price),
                "price_source": "public_contract_ticker",

                "min_notional_usdt": str(min_notional),
                "min_notional_is_estimate": bool(
                    specs.get("min_notional_is_estimate")
                ),
                "min_notional_source": specs.get(
                    "min_notional_source"
                ),

                "contract_multiplier": str(
                    specs.get("contract_multiplier")
                ),
                "min_token_qty": str(quantities["min_token_qty"]),
                "min_contract_qty": str(
                    quantities["min_contract_qty"]
                ),
                "contract_step": str(quantities["contract_step"]),
                "rounded_token_qty": str(
                    quantities["rounded_token_qty"]
                ),

                "account_leverage": str(account_lev),
                "contract_max_leverage": str(contract_lev),
                "effective_leverage_cap": str(allowed_lev),

                "minimum_margin_estimate_usdt": (
                    str(minimum_margin)
                    if minimum_margin is not None
                    else None
                ),

                "capital_only_feasible": bool(capital_only),
                "trading_eligible": False,
                "blockers": blockers,
            })

        except Exception as exc:
            reason = str(exc).replace("\n", " ")[:180]

            reports.append({
                "symbol": symbol,
                "capital_only_feasible": False,
                "trading_eligible": False,
                "blockers": [
                    f"contract_validation_failed:"
                    f"{type(exc).__name__}:{reason}"
                ],
            })

    positions = snapshot.get("positions")
    open_orders = snapshot.get("open_orders")

    if not isinstance(positions, list):
        raise CapitalReportError("positions_snapshot_invalid")

    if not isinstance(open_orders, list):
        raise CapitalReportError("open_orders_snapshot_invalid")

    account_clear = (
        snapshot.get("halt_live") is False
        and not positions
        and not open_orders
    )

    return {
        "schema": "apexquant_toobit_capital_feasibility_v2",
        "ok": True,
        "read_only": True,

        "account_clear": account_clear,
        "available_usdt_positive": available > 0,
        "available_balance_redacted": True,

        "risk_per_trade_fraction": str(risk_fraction),
        "margin_utilization_cap": str(margin_cap),
        "strategy_leverage_cap": str(leverage_cap),

        "ticker_prices_used": True,
        "estimated_minimums_are_not_exchange_confirmed": True,
        "minimum_margin_formula": "minimum_notional/effective_leverage",

        "fees_and_slippage_included": False,
        "funding_included": False,
        "stop_loss_risk_calculated": False,
        "risk_budget_redacted": True,

        "symbols_checked": len(reports),
        "capital_only_feasible_count": sum(
            1 for row in reports
            if row.get("capital_only_feasible") is True
        ),

        # This value deliberately remains zero.
        "trading_eligible_count": 0,
        "manual_authorization_required": True,
        "live_execution_enabled": False,

        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,

        "reports": reports,

        "global_blockers": [
            "capital_only_check_is_not_order_approval",
            "verified_user_fee_rate_required",
            "entry_and_stop_prices_required",
            "stop_loss_risk_budget_check_required",
            "fill_ledger_not_yet_integrated_with_v2_state",
            "partial_fill_and_timeout_recovery_required",
            "exchange_protection_integration_required",
            "live_execution_remains_disabled",
        ],
    }


def main():
    stage = "initialization"

    try:
        from toobit_client import ToobitClient
        from toobit_readonly_preflight import load_symbols

        stage = "client_initialization"
        client = ToobitClient()

        stage = "read_only_account_snapshot"
        snapshot = build_snapshot(client)

        stage = "public_exchange_contract_specs"
        exchange_info = fetch_exchange_info()

        stage = "public_ticker_prices"
        prices = fetch_ticker_prices()

        stage = "capital_feasibility_evaluation"
        report = evaluate_capital_feasibility(
            snapshot,
            exchange_info,
            load_symbols(),
            prices=prices,
            strategy_leverage_cap=STRATEGY_LEVERAGE_CAP,
            margin_utilization_cap=MARGIN_UTILIZATION_CAP,
            risk_per_trade=RISK_PER_TRADE,
        )

    except PreflightError as exc:
        report = {
            "schema": "apexquant_toobit_capital_feasibility_v2",
            "ok": False,
            "read_only": True,
            "stage": stage,
            "error_type": "PreflightError",
            "diagnostic": str(exc),
            "trading_eligible_count": 0,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
        }

    except Exception as exc:
        report = {
            "schema": "apexquant_toobit_capital_feasibility_v2",
            "ok": False,
            "read_only": True,
            "stage": stage,
            "error_type": type(exc).__name__,
            "diagnostic": str(exc)[:200],
            "trading_eligible_count": 0,
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
        }

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
