
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
        "contract_multiplier": "1",
        "min_qty": "0.001",
        "max_qty": "100",
        "qty_step": "0.001",
        "min_notional": "5",
        "max_leverage": "20",
    }
    result.update(overrides)
    return result


def run(**overrides):
    args = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "entry_price": "60000",
        "stop_price": "59000",
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
    assert float(result["estimated_stop_loss_and_cost_usdt"]) <= 1.0
    assert float(result["margin_utilization"]) <= 0.50


def test_small_wallet_cannot_force_minimum_quantity():
    result = run(available_usdt="1")
    assert result["eligible"] is False
    assert result["orders_submitted"] == 0


def test_long_stop_must_be_below_entry():
    result = run(stop_price="61000")
    assert result["eligible"] is False
    assert "long_stop_must_be_below_entry" in result["blockers"]


def test_short_stop_must_be_above_entry():
    result = run(side="SHORT", stop_price="59000")
    assert result["eligible"] is False
    assert "short_stop_must_be_above_entry" in result["blockers"]


def test_risk_fraction_hard_cap():
    result = run(risk_fraction="0.02")
    assert result["eligible"] is False
    assert "risk_fraction_exceeds_hard_cap" in result["blockers"]


def test_margin_cap_hard_limit():
    result = run(margin_utilization_cap="0.75")
    assert result["eligible"] is False


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
    assert "qty_step_must_be_positive" in result["blockers"]
