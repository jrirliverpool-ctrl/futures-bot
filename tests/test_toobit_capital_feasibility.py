
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "scripts"),
)

from symbol_specs import parse_symbol_specs
from toobit_capital_feasibility import (
    evaluate_capital_feasibility,
)


SYMBOLS = [
    "BTC-SWAP-USDT",
    "ETH-SWAP-USDT",
]


def make_snapshot(**overrides):
    result = {
        "read_only": True,
        "halt_live": False,
        "wallet": {
            "coin": "USDT",
            "availableBalance": "10",
        },
        "positions": [],
        "open_orders": [],
        "leverage_settings": [
            {
                "symbolId": "BTC-SWAP-USDT",
                "leverage": "3",
                "marginType": "ISOLATED",
            },
            {
                "symbolId": "ETH-SWAP-USDT",
                "leverage": "2",
                "marginType": "ISOLATED",
            },
        ],
    }

    result.update(overrides)
    return result


def make_contract(
    symbol,
    min_notional="5",
    max_leverage="20",
):
    return {
        "symbol": symbol,
        "status": "TRADING",
        "baseAsset": symbol.split("-")[0],
        "quoteAsset": "USDT",
        "marginToken": "USDT",
        "contractMultiplier": "1",
        "riskLimits": [
            {
                "maxLeverage": max_leverage,
            }
        ],
        "filters": [
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            },
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.001",
                "maxPrice": "1000000",
                "tickSize": "0.001",
            },
            {
                "filterType": "MIN_NOTIONAL",
                "minNotional": min_notional,
            },
        ],
    }


def make_exchange_info():
    return {
        "contracts": [
            make_contract("BTC-SWAP-USDT"),
            make_contract("ETH-SWAP-USDT"),
        ]
    }


def test_ten_usdt_report_is_read_only_and_never_approves_trading():
    result = evaluate_capital_feasibility(
        make_snapshot(),
        make_exchange_info(),
        SYMBOLS,
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["available_balance_redacted"] is True
    assert result["risk_budget_redacted"] is True

    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0
    assert result["trading_eligible_count"] == 0

    assert all(
        row["trading_eligible"] is False
        for row in result["reports"]
    )


def test_minimum_margin_uses_account_leverage_and_strategy_cap():
    result = evaluate_capital_feasibility(
        make_snapshot(),
        make_exchange_info(),
        SYMBOLS,
    )

    btc = result["reports"][0]
    eth = result["reports"][1]

    assert btc["effective_leverage_cap"] == "3"

    expected_btc_margin = (
        Decimal("5") / Decimal("3")
    )

    assert Decimal(
        btc["minimum_margin_estimate_usdt"]
    ) == expected_btc_margin

    assert eth["effective_leverage_cap"] == "2"
    assert eth["minimum_margin_estimate_usdt"] == "2.5"


def test_min_notional_primary_filter_is_used():
    contract = make_contract(
        "BTC-SWAP-USDT",
        min_notional="7.5",
    )

    specs = parse_symbol_specs(contract)

    assert specs["min_notional"] == 7.5
    assert (
        specs["min_notional_source"]
        == "MIN_NOTIONAL.minNotional"
    )


def test_trade_amount_min_amount_is_used_as_fallback():
    contract = make_contract("BTC-SWAP-USDT")

    contract["filters"] = [
        item
        for item in contract["filters"]
        if item["filterType"] != "MIN_NOTIONAL"
    ]

    contract["filters"].append({
        "filterType": "TRADE_AMOUNT",
        "minAmount": "10",
    })

    specs = parse_symbol_specs(contract)

    assert specs["min_notional"] == 10
    assert (
        specs["min_notional_source"]
        == "TRADE_AMOUNT.minAmount"
    )


def test_min_notional_has_priority_over_trade_amount():
    contract = make_contract(
        "BTC-SWAP-USDT",
        min_notional="5",
    )

    contract["filters"].append({
        "filterType": "TRADE_AMOUNT",
        "minAmount": "10",
    })

    specs = parse_symbol_specs(contract)

    assert specs["min_notional"] == 5
    assert (
        specs["min_notional_source"]
        == "MIN_NOTIONAL.minNotional"
    )


def test_missing_both_minimum_filters_is_rejected():
    contract = make_contract("BTC-SWAP-USDT")

    contract["filters"] = [
        item
        for item in contract["filters"]
        if item["filterType"] not in (
            "MIN_NOTIONAL",
            "TRADE_AMOUNT",
        )
    ]

    try:
        parse_symbol_specs(contract)
    except ValueError as exc:
        assert "minimum_order_value_missing" in str(exc)
        return

    raise AssertionError(
        "missing minimum filters must be rejected"
    )


def test_zero_trade_amount_is_rejected():
    contract = make_contract("BTC-SWAP-USDT")

    contract["filters"] = [
        item
        for item in contract["filters"]
        if item["filterType"] != "MIN_NOTIONAL"
    ]

    contract["filters"].append({
        "filterType": "TRADE_AMOUNT",
        "minAmount": "0",
    })

    try:
        parse_symbol_specs(contract)
    except ValueError as exc:
        assert "must be positive" in str(exc)
        return

    raise AssertionError(
        "zero minimum amount must be rejected"
    )


def test_invalid_trade_amount_is_rejected():
    contract = make_contract("BTC-SWAP-USDT")

    contract["filters"] = [
        item
        for item in contract["filters"]
        if item["filterType"] != "MIN_NOTIONAL"
    ]

    contract["filters"].append({
        "filterType": "TRADE_AMOUNT",
        "minAmount": "not-a-number",
    })

    try:
        parse_symbol_specs(contract)
    except ValueError as exc:
        assert "not numeric" in str(exc)
        return

    raise AssertionError(
        "invalid minimum amount must be rejected"
    )


def test_duplicate_minimum_filter_is_rejected():
    contract = make_contract("BTC-SWAP-USDT")

    contract["filters"].append({
        "filterType": "MIN_NOTIONAL",
        "minNotional": "5",
    })

    try:
        parse_symbol_specs(contract)
    except ValueError as exc:
        assert "duplicate_MIN_NOTIONAL_filter" in str(exc)
        return

    raise AssertionError(
        "duplicate minimum filters must be rejected"
    )


def test_zero_available_balance_does_not_pass_capital_check():
    snapshot = make_snapshot()
    snapshot["wallet"]["availableBalance"] = "0"

    result = evaluate_capital_feasibility(
        snapshot,
        make_exchange_info(),
        SYMBOLS,
    )

    assert result["ok"] is True
    assert result["available_usdt_positive"] is False

    assert all(
        row["capital_only_feasible"] is False
        for row in result["reports"]
    )


def test_minimum_order_margin_above_budget_is_blocked():
    exchange_info = {
        "contracts": [
            make_contract(
                "BTC-SWAP-USDT",
                min_notional="100",
            ),
            make_contract(
                "ETH-SWAP-USDT",
                min_notional="100",
            ),
        ]
    }

    result = evaluate_capital_feasibility(
        make_snapshot(),
        exchange_info,
        SYMBOLS,
    )

    assert all(
        row["capital_only_feasible"] is False
        for row in result["reports"]
    )

    assert all(
        "minimum_order_margin_exceeds_budget" in row["blockers"]
        for row in result["reports"]
    )


def test_non_trading_contract_is_blocked():
    exchange_info = make_exchange_info()
    exchange_info["contracts"][0]["status"] = "HALT"

    result = evaluate_capital_feasibility(
        make_snapshot(),
        exchange_info,
        SYMBOLS,
    )

    assert result["reports"][0]["capital_only_feasible"] is False
    assert (
        "contract_not_trading"
        in result["reports"][0]["blockers"]
    )


def test_missing_contract_is_blocked_without_raising():
    exchange_info = {
        "contracts": [
            make_contract("BTC-SWAP-USDT"),
        ]
    }

    result = evaluate_capital_feasibility(
        make_snapshot(),
        exchange_info,
        SYMBOLS,
    )

    assert result["reports"][0]["capital_only_feasible"] is True
    assert result["reports"][1]["capital_only_feasible"] is False

    assert (
        "contract_not_found"
        in result["reports"][1]["blockers"]
    )


def test_account_position_or_open_order_marks_account_not_clear():
    snapshot = make_snapshot(
        positions=[
            {
                "symbol": "BTC-SWAP-USDT",
                "position": "1",
            }
        ]
    )

    result = evaluate_capital_feasibility(
        snapshot,
        make_exchange_info(),
        SYMBOLS,
    )

    assert result["account_clear"] is False
    assert result["trading_eligible_count"] == 0


def test_invalid_snapshot_is_rejected():
    try:
        evaluate_capital_feasibility(
            {"read_only": False},
            make_exchange_info(),
            SYMBOLS,
        )
    except RuntimeError:
        return

    raise AssertionError(
        "invalid snapshot should fail closed"
    )


def test_missing_account_leverage_coverage_is_rejected():
    snapshot = make_snapshot()
    snapshot["leverage_settings"] = (
        snapshot["leverage_settings"][:1]
    )

    try:
        evaluate_capital_feasibility(
            snapshot,
            make_exchange_info(),
            SYMBOLS,
        )
    except RuntimeError:
        return

    raise AssertionError(
        "missing leverage coverage should fail closed"
    )
