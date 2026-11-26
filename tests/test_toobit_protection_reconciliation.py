
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from toobit_protection_reconciliation import (
    reconcile_protective_orders,
)


SYMBOL = "BTC-SWAP-USDT"
_SENTINEL = object()


def position(**overrides):
    row = {
        "symbol": SYMBOL,
        "side": "LONG",
        "position": "2",
        "available": "2",
    }
    row.update(overrides)
    return row


def stop_loss(**overrides):
    row = {
        "orderId": "1001",
        "symbol": SYMBOL,
        "side": "SELL",
        "positionSide": "LONG",
        "status": "ORDER_NEW",
        "origQty": "2",
        "stopPrice": "95000",
        "stopOrderType": "STOP_LOSS",
        "stopType": "FIXED_STOP",
    }
    row.update(overrides)
    return row


def snapshot(orders=_SENTINEL, **overrides):
    data = {
        "orders": (
            [stop_loss()] if orders is _SENTINEL else orders
        ),
        "stable": True,
        "complete_within_documented_page_bound": True,
        "read_only": True,
        "network_methods_used": ["GET"],
        "categories_checked": [
            "STOP",
            "STOP_PROFIT_LOSS",
        ],
        "scans": 2,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
    }
    data.update(overrides)
    return data


def run(positions=None, algo=None):
    return reconcile_protective_orders(
        [position()] if positions is None else positions,
        snapshot() if algo is None else algo,
        [SYMBOL],
    )


def test_full_size_matching_long_stop_passes():
    result = run()

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["protected_positions"] == 1
    assert result["unprotected_positions"] == 0

    report = result["position_reports"][f"{SYMBOL}:LONG"]
    assert report["coverage_mode"] == "FIXED_CONFIGURED_QUANTITY"
    assert report["verified_coverage_qty"] == "2"


def test_short_position_requires_buy_stop_loss():
    result = run(
        positions=[
            position(side="SHORT"),
        ],
        algo=snapshot([
            stop_loss(
                side="BUY",
                positionSide="SHORT",
            ),
        ]),
    )

    assert result["ok"] is True


def test_wrong_closing_side_fails():
    result = run(
        algo=snapshot([
            stop_loss(side="BUY"),
        ]),
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_wrong_position_side_fails():
    result = run(
        algo=snapshot([
            stop_loss(positionSide="SHORT"),
        ]),
    )

    assert result["ok"] is False


def test_take_profit_is_not_stop_loss_protection():
    tp = stop_loss(
        stopOrderType="TAKE_PROFIT",
        orderId="1002",
    )

    result = run(algo=snapshot([tp]))

    assert result["ok"] is False
    assert (
        result["position_reports"][f"{SYMBOL}:LONG"]["reason"]
        == "protective_stop_loss_missing"
    )


def test_fixed_stop_quantity_must_cover_position():
    result = run(
        algo=snapshot([
            stop_loss(origQty="1.5"),
        ]),
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][f"{SYMBOL}:LONG"]["reason"]
        == "protective_stop_quantity_insufficient"
    )


def test_executed_qty_is_not_required_for_open_algo_order():
    result = run(
        algo=snapshot([
            stop_loss(),
        ]),
    )

    assert result["ok"] is True


def test_nonzero_executed_qty_on_active_order_fails_closed():
    result = run(
        algo=snapshot([
            stop_loss(executedQty="0.5"),
        ]),
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_full_position_tpsl_zero_quantity_is_supported():
    result = run(
        algo=snapshot([
            stop_loss(origQty="0"),
        ]),
    )

    assert result["ok"] is True

    report = result["position_reports"][f"{SYMBOL}:LONG"]
    assert (
        report["coverage_mode"]
        == "FULL_AVAILABLE_POSITION_AT_TRIGGER"
    )
    assert report["verified_coverage_qty"] == "2"


def test_full_position_tpsl_requires_available_quantity():
    result = run(
        positions=[
            {
                "symbol": SYMBOL,
                "side": "LONG",
                "position": "2",
            },
        ],
        algo=snapshot([
            stop_loss(origQty="0"),
        ]),
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][f"{SYMBOL}:LONG"]["reason"]
        == "full_position_stop_requires_available_quantity"
    )


def test_full_position_tpsl_fails_when_available_is_less_than_position():
    result = run(
        positions=[
            position(available="1.5"),
        ],
        algo=snapshot([
            stop_loss(origQty="0"),
        ]),
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][f"{SYMBOL}:LONG"]["reason"]
        == "full_position_stop_available_quantity_insufficient"
    )


def test_fixed_stop_fails_when_available_is_insufficient():
    result = run(
        positions=[
            position(available="1"),
        ],
        algo=snapshot([
            stop_loss(origQty="2"),
        ]),
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][f"{SYMBOL}:LONG"]["reason"]
        == "position_available_quantity_insufficient"
    )


def test_trailing_stop_accepts_zero_stop_price_with_callback():
    trailing = stop_loss(
        stopType="TRAILING_STOP",
        stopPrice="0",
        fallbackType="RATE",
        fallbackQuantity="1",
        activePrice="0",
        activeStatus=0,
    )

    result = run(algo=snapshot([trailing]))

    assert result["ok"] is True

    report = result["position_reports"][f"{SYMBOL}:LONG"]
    assert report["stop_type"] == "TRAILING_STOP"


@pytest.mark.parametrize(
    "bad",
    [
        stop_loss(stopPrice="0"),
        stop_loss(origQty="NaN"),
        stop_loss(orderId="bad"),
        stop_loss(positionSide="BOTH"),
        stop_loss(
            stopType="TRAILING_STOP",
            stopPrice="0",
            fallbackType="UNKNOWN",
            fallbackQuantity="1",
        ),
        stop_loss(
            stopType="TRAILING_STOP",
            stopPrice="0",
            fallbackType="RATE",
            fallbackQuantity="0",
        ),
    ],
)
def test_invalid_stop_loss_fails_closed(bad):
    result = run(algo=snapshot([bad]))

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_multiple_matching_stop_losses_fail_as_ambiguous():
    result = run(
        algo=snapshot([
            stop_loss(),
            stop_loss(orderId="1002"),
        ]),
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][f"{SYMBOL}:LONG"]["reason"]
        == "multiple_matching_stop_losses"
    )


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        snapshot(stable=False),
        snapshot(read_only=False),
        snapshot(scans=1),
        snapshot(categories_checked=["STOP"]),
        snapshot(orders=None),
        snapshot(complete_within_documented_page_bound=False),
        snapshot(network_methods_used=["GET", "POST"]),
        snapshot(orders_submitted=1),
    ],
)
def test_untrusted_snapshot_fails_closed(bad_snapshot):
    result = run(algo=bad_snapshot)

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_flat_account_needs_no_protective_orders():
    result = run(
        positions=[
            position(
                position="0",
                available="0",
            ),
        ],
        algo=snapshot([]),
    )

    assert result["ok"] is True
    assert result["positions_checked"] == 0


def test_unconfigured_active_position_fails():
    result = reconcile_protective_orders(
        [
            position(),
            position(
                symbol="ETH-SWAP-USDT",
                side="SHORT",
                position="1",
                available="1",
            ),
        ],
        snapshot(),
        [SYMBOL],
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_duplicate_active_position_fails():
    result = run(
        positions=[
            position(),
            position(),
        ],
    )

    assert result["ok"] is False


def test_available_quantity_cannot_exceed_position():
    result = run(
        positions=[
            position(available="3"),
        ],
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_no_order_mutation_is_reported():
    result = run()

    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0
    assert result["state_mutated"] is False
