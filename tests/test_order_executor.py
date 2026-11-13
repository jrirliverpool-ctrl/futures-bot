
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from order_executor import (
    OrderRequest,
    evaluate,
    DEFAULT_CONFIG,
)


BASE_SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.1,
    "max_qty": 100.0,
    "qty_step": 0.1,
    "min_notional": 0.01,
    "min_notional_verified": True,
    "tick_size": 0.1,
    "max_leverage": 10,
}


def _req(**kwargs):
    data = {
        "symbol": "BTC-SWAP-USDT",
        "side": "LONG",
        "order_type": "MARKET",
        "qty": 1.0,
        "price": None,
        "leverage": 3,
        "intent": "ENTRY",
    }
    data.update(kwargs)
    return OrderRequest(**data)


def test_entry_valid_with_official_minimum():
    result = evaluate(
        _req(),
        BASE_SPECS,
        {},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is True


def test_entry_rejected_when_minimum_verification_missing():
    specs = dict(BASE_SPECS)
    specs.pop("min_notional_verified")

    result = evaluate(
        _req(),
        specs,
        {},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False
    assert result.gate == "notional"
    assert result.reason == "minimum_order_value_unverified"


def test_entry_rejected_when_minimum_is_unverified():
    specs = dict(BASE_SPECS)
    specs["min_notional_verified"] = False

    result = evaluate(
        _req(),
        specs,
        {},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False
    assert result.reason == "minimum_order_value_unverified"


def test_entry_rejected_when_verified_minimum_is_missing():
    specs = dict(BASE_SPECS)
    specs["min_notional"] = None

    result = evaluate(
        _req(),
        specs,
        {},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_specs_nan_rejected():
    specs = dict(BASE_SPECS)
    specs["min_qty"] = float("nan")

    result = evaluate(
        _req(),
        specs,
        {},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_specs_bool_rejected():
    specs = dict(BASE_SPECS)
    specs["qty_step"] = True

    result = evaluate(
        _req(),
        specs,
        {},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_exit_not_blocked_by_missing_entry_minimum_verification():
    specs = dict(BASE_SPECS)
    specs.pop("min_notional_verified")

    state = {
        "position": {
            "side": "LONG",
            "remaining_size": 1.0,
        }
    }

    result = evaluate(
        _req(intent="EXIT"),
        specs,
        state,
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is True


def test_exit_wrong_side_rejected():
    state = {
        "position": {
            "side": "LONG",
            "remaining_size": 1.0,
        }
    }

    result = evaluate(
        _req(intent="EXIT", side="SHORT"),
        BASE_SPECS,
        state,
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_exit_position_side_missing_rejected():
    state = {
        "position": {
            "remaining_size": 1.0,
        }
    }

    result = evaluate(
        _req(intent="EXIT"),
        BASE_SPECS,
        state,
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_halt_blocks_entry():
    result = evaluate(
        _req(),
        BASE_SPECS,
        {"halt_trading": True},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_pending_order_id_blocks_entry():
    result = evaluate(
        _req(),
        BASE_SPECS,
        {"pending_order_id": "apex_123"},
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is False


def test_tp1_hit_valid():
    state = {
        "position": {
            "side": "LONG",
            "remaining_size": 1.0,
        }
    }

    result = evaluate(
        _req(intent="TP1_HIT", side="LONG", qty=0.5),
        BASE_SPECS,
        state,
        DEFAULT_CONFIG,
        ref_price=100.0,
    )

    assert result.accepted is True
