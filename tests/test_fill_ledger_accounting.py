
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from fill_ledger import reconcile_order_fills


def make_trade(
    trade_id,
    qty,
    price,
    commission,
    maker_rebate,
    realized_pnl,
):
    return {
        "time": "1790000000123",
        "id": trade_id,
        "orderId": "order-accounting-1",
        "symbol": "BTC-SWAP-USDT",
        "price": str(price),
        "qty": str(qty),
        "commissionAsset": "USDT",
        "commission": str(commission),
        "makerRebate": str(maker_rebate),
        "type": "LIMIT",
        "isMaker": True,
        "side": "BUY_OPEN",
        "realizedPnl": str(realized_pnl),
        "ticketId": "ticket-accounting-1",
    }


def make_order():
    return {
        "orderId": "order-accounting-1",
        "clientOrderId": "client-accounting-1",
        "symbol": "BTC-SWAP-USDT",
        "side": "BUY_OPEN",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "106",
        "status": "FILLED",
    }


def test_reconciliation_reports_realized_pnl_and_maker_rebates():
    fills = [
        make_trade(
            "trade-accounting-1",
            "0.4",
            "100",
            "0.01",
            "0.002",
            "1.25",
        ),
        make_trade(
            "trade-accounting-2",
            "0.6",
            "110",
            "0.02",
            "0.003",
            "-0.50",
        ),
    ]

    result = reconcile_order_fills(make_order(), fills)

    assert result["ok"] is True
    assert result["halt_trading"] is False

    assert result["fill_count"] == 2
    assert result["weighted_avg_price"] == 106.0

    # Existing commission output remains compatible.
    assert result["commissions"]["USDT"] == 0.03

    # New exact-decimal accounting outputs.
    assert result["maker_rebates"]["USDT"] == "0.005"
    assert result["realized_pnl"] == "0.75"


def test_empty_fill_set_has_zero_accounting_totals():
    order = {
        "orderId": "order-accounting-2",
        "clientOrderId": "client-accounting-2",
        "symbol": "BTC-SWAP-USDT",
        "side": "BUY_OPEN",
        "origQty": "1",
        "executedQty": "0",
        "avgPrice": "0",
        "status": "NEW",
    }

    result = reconcile_order_fills(order, [])

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["maker_rebates"] == {}
    assert result["realized_pnl"] == "0"
    assert result["fill_count"] == 0
