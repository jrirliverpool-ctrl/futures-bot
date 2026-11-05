
"""Read-only capital feasibility report for Toobit USDT-M futures.

This diagnostic reads account balance, configured contract specifications,
and account leverage. It NEVER submits, amends, or cancels orders.

A capital-only pass is NOT an order approval.
LIVE execution remains disabled.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(__file__))

from symbol_specs import fetch_exchange_info, parse_symbol_specs
from toobit_readonly_preflight import build_snapshot, PreflightError


STRATEGY_LEVERAGE_CAP = Decimal("3")
MARGIN_UTILIZATION_CAP = Decimal("0.50")
RISK_PER_TRADE = Decimal("0.001")


class CapitalReportError(RuntimeError):
    """Safe diagnostic error for the capital feasibility report."""


def _decimal(value, field, *, allow_zero=False):
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


def _contract_max_leverage(raw_contract, specs, symbol):
    """
    Read the contract leverage ceiling.

    Some exchangeInfo responses expose maxLeverage directly.
    The official Toobit exchangeInfo example exposes initialMargin
    in riskLimits instead. In that case, derive the tier-one ceiling
    as 1 / initialMargin.

    This is only a contract ceiling estimate. The final effective
    leverage is still capped by account and strategy leverage.
    """
    direct_value = specs.get("max_leverage")

    if direct_value is not None:
        return _decimal(
            direct_value,
            f"{symbol}_contract_max_leverage",
        )

    risk_limits = raw_contract.get("riskLimits")

    if not isinstance(risk_limits, list) or not risk_limits:
        raise ValueError(f"{symbol}_risk_limits_missing")

    first_tier = risk_limits[0]

    if not isinstance(first_tier, dict):
        raise ValueError(f"{symbol}_risk_tier_invalid")

    initial_margin = _decimal(
        first_tier.get("initialMargin"),
        f"{symbol}_initial_margin",
    )

    if initial_margin > Decimal("1"):
        raise ValueError(f"{symbol}_initial_margin_out_of_range")

    return Decimal("1") / initial_margin


def _contract_map(exchange_info):
    if not isinstance(exchange_info, dict):
        raise CapitalReportError("exchange_info_invalid")

    contracts = exchange_info.get("contracts")

    if not isinstance(contracts, list):
        raise CapitalReportError("exchange_contracts_missing")

    result = {}

    for contract in contracts:
        if not isinstance(contract, dict):
            raise CapitalReportError("exchange_contract_row_invalid")

        symbol = contract.get("symbol")

        if not isinstance(symbol, str) or not symbol:
            raise CapitalReportError("exchange_contract_symbol_invalid")

        if symbol in result:
            raise CapitalReportError("duplicate_exchange_contract")

        result[symbol] = contract

    return result


def _leverage_map(rows, symbols):
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

        result[symbol] = _decimal(
            row.get("leverage"),
            f"{symbol}_account_leverage",
        )

    missing = set(symbols) - set(result)

    if missing:
        raise CapitalReportError("account_leverage_coverage_incomplete")

    return result


def evaluate_capital_feasibility(
    snapshot,
    exchange_info,
    symbols,
    *,
    strategy_leverage_cap="3",
    margin_utilization_cap="0.50",
    risk_per_trade="0.001",
):
    """Evaluate minimum-order margin feasibility without authorizing trades."""

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

    available = _decimal(
        wallet.get("availableBalance"),
        "available_usdt",
        allow_zero=True,
    )

    leverage_cap = _decimal(
        strategy_leverage_cap,
        "strategy_leverage_cap",
    )

    margin_cap = _decimal(
        margin_utilization_cap,
        "margin_utilization_cap",
    )

    risk_fraction = _decimal(
        risk_per_trade,
        "risk_per_trade",
    )

    if leverage_cap > Decimal("3"):
        raise CapitalReportError("strategy_leverage_cap_exceeds_hard_cap")

    if margin_cap > Decimal("0.50"):
        raise CapitalReportError("margin_utilization_cap_exceeds_hard_cap")

    if margin_cap > Decimal("1"):
        raise CapitalReportError("margin_utilization_cap_invalid")

    if risk_fraction > Decimal("0.01"):
        raise CapitalReportError("risk_fraction_exceeds_hard_cap")

    contracts = _contract_map(exchange_info)

    account_leverage = _leverage_map(
        snapshot.get("leverage_settings"),
        symbols,
    )

    margin_budget = available * margin_cap

    reports = []

    for symbol in symbols:
        blockers = []
        raw_contract = contracts.get(symbol)

        if raw_contract is None:
            reports.append({
                "symbol": symbol,
                "capital_only_feasible": False,
                "trading_eligible": False,
                "blockers": ["contract_not_found"],
            })
            continue

        try:
            specs = parse_symbol_specs(raw_contract)

            if specs.get("symbol") != symbol:
                raise ValueError("contract_symbol_mismatch")

            if specs.get("status") != "TRADING":
                blockers.append("contract_not_trading")

            min_notional = _decimal(
                specs.get("min_notional"),
                f"{symbol}_min_notional",
            )

            contract_max_leverage = _contract_max_leverage(
                raw_contract,
                specs,
                symbol,
            )

            account_lev = account_leverage[symbol]

            allowed_leverage = min(
                leverage_cap,
                contract_max_leverage,
                account_lev,
            )

            if allowed_leverage <= 0:
                blockers.append("effective_leverage_invalid")

            minimum_margin_estimate = None
            margin_fits = False

            if not blockers:
                minimum_margin_estimate = (
                    min_notional / allowed_leverage
                )

                margin_fits = (
                    available > 0
                    and minimum_margin_estimate <= margin_budget
                )

                if not margin_fits:
                    blockers.append(
                        "minimum_order_margin_exceeds_budget"
                    )

            # These checks are still mandatory before any real order.
            blockers.extend([
                "verified_user_fee_rate_required",
                "entry_and_stop_prices_required",
                "actual_order_risk_precheck_required",
                "fill_reconciliation_and_protection_required",
            ])

            # Capital feasibility is NOT trade authorization.
            capital_only_feasible = (
                bool(margin_fits)
                and "contract_not_trading" not in blockers
                and "effective_leverage_invalid" not in blockers
                and "minimum_order_margin_exceeds_budget" not in blockers
            )

            reports.append({
                "symbol": symbol,
                "contract_status": specs.get("status"),
                "min_notional_usdt": str(min_notional),
                "contract_multiplier": str(
                    specs.get("contract_multiplier")
                ),
                "min_qty": str(specs.get("min_qty")),
                "qty_step": str(specs.get("qty_step")),
                "account_leverage": str(account_lev),
                "contract_max_leverage": str(contract_max_leverage),
                "effective_leverage_cap": str(allowed_leverage),
                "minimum_margin_estimate_usdt": (
                    str(minimum_margin_estimate)
                    if minimum_margin_estimate is not None
                    else None
                ),
                "capital_only_feasible": capital_only_feasible,
                "trading_eligible": False,
                "blockers": blockers,
            })

        except Exception as exc:
            # Keep the report useful without exposing account secrets.
            # Include the validation reason so future failures are diagnosable.
            reason = str(exc).replace("\n", " ")[:160]

            reports.append({
                "symbol": symbol,
                "capital_only_feasible": False,
                "trading_eligible": False,
                "blockers": [
                    f"contract_validation_failed:{type(exc).__name__}:{reason}"
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
        "schema": "apexquant_toobit_capital_feasibility_v1",
        "ok": True,
        "read_only": True,
        "account_clear": account_clear,
        "available_usdt_positive": available > 0,
        "available_balance_redacted": True,
        "risk_per_trade_fraction": str(risk_fraction),
        "margin_utilization_cap": str(margin_cap),
        "strategy_leverage_cap": str(leverage_cap),
        "minimum_margin_estimates_are_approximate": True,
        "minimum_margin_formula": "min_notional / effective_leverage",
        "fees_and_slippage_included": False,
        "funding_included": False,
        "risk_budget_redacted": True,
        "symbols_checked": len(reports),
        "capital_only_feasible_count": sum(
            1 for row in reports
            if row.get("capital_only_feasible") is True
        ),
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
            "fill_ledger_not_yet_integrated_with_v2_state",
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

        stage = "capital_feasibility_evaluation"
        report = evaluate_capital_feasibility(
            snapshot,
            exchange_info,
            load_symbols(),
            strategy_leverage_cap=STRATEGY_LEVERAGE_CAP,
            margin_utilization_cap=MARGIN_UTILIZATION_CAP,
            risk_per_trade=RISK_PER_TRADE,
        )

    except PreflightError as exc:
        report = {
            "schema": "apexquant_toobit_capital_feasibility_v1",
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
            "schema": "apexquant_toobit_capital_feasibility_v1",
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
