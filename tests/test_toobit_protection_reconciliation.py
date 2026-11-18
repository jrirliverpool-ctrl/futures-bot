
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

from toobit_protection_reconciliation import (
    reconcile_protective_orders,
)


SYMBOL = "BTC-SWAP-USDT"


def position(**overrides):
    row = {
        "symbol": SYMBOL,
        "side": "LONG",
        "position": "2",
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
        "executedQty": "0",
        "stopPrice": "95000",
        "stopOrderType": "STOP_LOSS",
        "stopType": "FIXED_STOP",
    }
    row.update(overrides)
    return row


def snapshot(orders=None, **overrides):
    data = {
        "orders": [stop_loss()] if orders is None else orders,
        "stable": True,
        "read_only": True,
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


def test_full_size_matching_long_stop_loss_passes():
    result = run()

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["protected_positions"] == 1
    assert result["unprotected_positions"] == 0
    assert result["read_only"] is True


def test_short_position_requires_buy_stop_loss():
    result = run(
        positions=[
            position(side="SHORT")
        ],
        algo=snapshot([
            stop_loss(
                side="BUY",
                positionSide="SHORT",
            )
        ]),
    )

    assert result["ok"] is True


def test_wrong_closing_side_fails():
    result = run(
        algo=snapshot([
            stop_loss(side="BUY")
        ])
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_wrong_position_side_fails():
    result = run(
        algo=snapshot([
            stop_loss(positionSide="SHORT")
        ])
    )

    assert result["ok"] is False


def test_missing_stop_loss_fails_even_with_take_profit():
    tp = stop_loss(
        stopOrderType="TAKE_PROFIT",
        orderId="1002",
    )

    result = run(algo=snapshot([tp]))

    assert result["ok"] is False
    assert (
        result["position_reports"][
            f"{SYMBOL}:LONG"
        ]["reason"]
        == "protective_stop_loss_missing"
    )


def test_insufficient_remaining_stop_quantity_fails():
    result = run(
        algo=snapshot([
            stop_loss(
                origQty="2",
                executedQty="0.5",
            )
        ])
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][
            f"{SYMBOL}:LONG"
        ]["reason"]
        == "protective_stop_quantity_insufficient"
    )


def test_multiple_matching_stop_losses_fail_as_ambiguous():
    result = run(
        algo=snapshot([
            stop_loss(orderId="1001"),
            stop_loss(orderId="1002"),
        ])
    )

    assert result["ok"] is False
    assert (
        result["position_reports"][
            f"{SYMBOL}:LONG"
        ]["reason"]
        == "multiple_matching_stop_losses"
    )


@pytest.mark.parametrize(
    "bad",
    [
        stop_loss(stopPrice="0"),
        stop_loss(origQty="NaN"),
        stop_loss(executedQty="3"),
        stop_loss(orderId="bad"),
    ],
)
def test_invalid_stop_loss_fails_closed(bad):
    result = run(algo=snapshot([bad]))

    assert result["ok"] is False
    assert result["halt_trading"] is True


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        snapshot(stable=False),
        snapshot(read_only=False),
        snapshot(scans=1),
        snapshot(categories_checked=["STOP"]),
        snapshot(orders=None),
    ],
)
def test_untrusted_snapshot_fails_closed(bad_snapshot):
    result = run(algo=bad_snapshot)

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_flat_account_needs_no_protective_orders():
    result = run(
        positions=[
            position(position="0")
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
        ]
    )

    assert result["ok"] is False


def test_no_order_mutation_is_reported():
    result = run()

    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0
    assert result["state_mutated"] is False
