
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "scripts"),
)

from live_risk_precheck import precheck_order


def specs(**overrides):
    result = {
        "symbol": "BTC-SWAP-USDT",
        "status": "TRADING",
        "quantity_unit": "contracts",
        "contract_multiplier": "1",
        "min_qty": "0.001",
        "max_qty": "100",
        "qty_step": "0.001",
        "min_notional": "5",
        "min_notional_verified": True,
        "max_leverage": "20",
    }
    result.update(overrides)
    return result


def run(**overrides):
    args = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "entry_price": "60000",
        "stop_price": "59500",
        "available_usdt": "1000",
        "risk_fraction": "0.001",
        "leverage": "3",
        "contract_specs": specs(),
        "fee_rate_per_side": "0.0006",
        "slippage_rate_per_side": "0.0005",
    }
    args.update(overrides)
    return precheck_order(**args)


def test_valid_order_respects_risk_and_margin():
    result = run()

    assert result["eligible"] is True
    assert result["orders_submitted"] == 0
    assert result["quantity_unit"] == "contracts"
    assert float(result["estimated_stop_loss_and_cost_usdt"]) <= 1.0
    assert float(result["margin_utilization"]) <= 0.50


def test_missing_minimum_verification_is_rejected():
    contract_specs = specs()
    contract_specs.pop("min_notional_verified")

    result = run(contract_specs=contract_specs)

    assert result["eligible"] is False
    assert "min_notional_unverified" in result["blockers"]
    assert result["orders_submitted"] == 0


def test_false_minimum_verification_is_rejected():
    result = run(
        contract_specs=specs(min_notional_verified=False)
    )

    assert result["eligible"] is False
    assert "min_notional_unverified" in result["blockers"]


def test_small_wallet_cannot_force_minimum_quantity():
    result = run(available_usdt="1")

    assert result["eligible"] is False
    assert result["orders_submitted"] == 0


def test_long_stop_must_be_below_entry():
    result = run(stop_price="61000")

    assert result["eligible"] is False
    assert "long_stop_must_be_below_entry" in result["blockers"]


def test_short_stop_must_be_above_entry():
    result = run(
        side="SHORT",
        stop_price="59000",
    )

    assert result["eligible"] is False
    assert "short_stop_must_be_above_entry" in result["blockers"]


def test_risk_fraction_hard_cap():
    result = run(risk_fraction="0.02")

    assert result["eligible"] is False
    assert "risk_fraction_exceeds_hard_cap" in result["blockers"]


def test_margin_cap_hard_limit():
    result = run(margin_utilization_cap="0.75")

    assert result["eligible"] is False
    assert "margin_cap_exceeds_hard_cap" in result["blockers"]


def test_symbol_mismatch_is_rejected():
    result = run(
        contract_specs=specs(symbol="ETH-SWAP-USDT")
    )

    assert result["eligible"] is False
    assert "symbol_specs_mismatch" in result["blockers"]


def test_inactive_contract_is_rejected():
    result = run(
        contract_specs=specs(status="HALT")
    )

    assert result["eligible"] is False
    assert "contract_not_trading" in result["blockers"]


def test_missing_fee_rate_is_rejected():
    result = run(fee_rate_per_side=None)

    assert result["eligible"] is False
    assert "fee_rate_invalid" in result["blockers"]


def test_zero_quantity_step_is_rejected():
    result = run(contract_specs=specs(qty_step="0"))

    assert result["eligible"] is False
    assert "qty_step_invalid" in result["blockers"]


def test_wrong_quantity_unit_is_rejected():
    result = run(
        contract_specs=specs(quantity_unit="tokens")
    )

    assert result["eligible"] is False
    assert "quantity_unit_must_be_contracts" in result["blockers"]


def test_missing_quantity_unit_is_rejected():
    contract_specs = specs()
    contract_specs.pop("quantity_unit")

    result = run(contract_specs=contract_specs)

    assert result["eligible"] is False
    assert "quantity_unit_must_be_contracts" in result["blockers"]


def test_leverage_above_absolute_cap_is_rejected():
    result = run(leverage="4")

    assert result["eligible"] is False
    assert "leverage_exceeds_hard_cap" in result["blockers"]


def test_configured_leverage_cap_cannot_exceed_hard_cap():
    result = run(leverage_cap="5")

    assert result["eligible"] is False
    assert "leverage_cap_exceeds_hard_cap" in result["blockers"]


def test_cost_uses_stop_price_for_exit():
    result = run()

    quantity = float(result["quantity"])
    multiplier = 1.0
    stop = 59500.0
    fee_plus_slippage = 0.0011

    expected_exit_cost = (
        quantity
        * stop
        * multiplier
        * fee_plus_slippage
    )

    assert abs(
        float(result["estimated_exit_cost_usdt"])
        - expected_exit_cost
    ) < 1e-8


def test_short_valid_order():
    result = run(
        side="SHORT",
        entry_price="60000",
        stop_price="60500",
    )

    assert result["eligible"] is True


def test_boolean_numeric_input_is_rejected():
    result = run(entry_price=True)

    assert result["eligible"] is False
    assert "entry_price_invalid" in result["blockers"]


def test_zero_minimum_notional_is_rejected():
    result = run(
        contract_specs=specs(min_notional="0")
    )

    assert result["eligible"] is False
    assert "min_notional_invalid" in result["blockers"]
