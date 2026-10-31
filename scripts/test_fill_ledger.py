import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from fill_ledger import (
    append_user_trades,
    load_fill_ledger,
    normalize_user_trade,
    reconcile_order_fills,
)


def trade(**overrides):
    row = {
        "time": "1790000000123",
        "id": "trade-1",
        "orderId": "order-1",
        "symbol": "BTC-SWAP-USDT",
        "price": "100",
        "qty": "0.4",
        "commissionAsset": "USDT",
        "commission": "0.01",
        "makerRebate": "0",
        "type": "LIMIT",
        "isMaker": False,
        "side": "BUY_OPEN",
        "realizedPnl": "0",
        "ticketId": "ticket-1",
    }
    row.update(overrides)
    return row


def order(**overrides):
    row = {
        "orderId": "order-1",
        "clientOrderId": "client-1",
        "symbol": "BTC-SWAP-USDT",
        "side": "BUY_OPEN",
        "origQty": "1",
        "executedQty": "0.4",
        "avgPrice": "100",
        "status": "PARTIALLY_FILLED",
    }
    row.update(overrides)
    return row


def test_normalize_trade_preserves_ids_and_decimal_values():
    row = normalize_user_trade(trade())
    assert row["trade_id"] == "trade-1"
    assert row["order_id"] == "order-1"
    assert row["qty"] == "0.4"
    assert row["commission_asset"] == "USDT"


@pytest.mark.parametrize("field,value", [
    ("id", None), ("orderId", None), ("symbol", ""),
    ("qty", "0"), ("qty", "NaN"), ("price", "-1"),
    ("commission", "inf"), ("side", "BAD"),
    ("commissionAsset", ""), ("time", "not-a-time"),
])
def test_invalid_trade_rejected(field, value):
    row = trade()
    row[field] = value
    with pytest.raises(ValueError):
        normalize_user_trade(row)


def test_expected_order_and_symbol_are_enforced():
    with pytest.raises(ValueError, match="symbol"):
        normalize_user_trade(trade(), expected_symbol="ETH-SWAP-USDT")
    with pytest.raises(ValueError, match="orderId"):
        normalize_user_trade(trade(), expected_order_id="other")


def test_append_is_idempotent_and_loads_jsonl(tmp_path):
    path = str(tmp_path / "fills.jsonl")
    appended, skipped = append_user_trades(path, [trade(), trade()])
    assert (appended, skipped) == (1, 1)
    rows = load_fill_ledger(path)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "trade-1"


def test_conflicting_duplicate_trade_id_rejected(tmp_path):
    path = str(tmp_path / "fills.jsonl")
    append_user_trades(path, [trade()])
    with pytest.raises(ValueError, match="conflicting"):
        append_user_trades(path, [trade(price="101")])


def test_corrupt_jsonl_fails_closed(tmp_path):
    path = tmp_path / "fills.jsonl"
    path.write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSONL"):
        load_fill_ledger(str(path))


def test_partial_fill_reconciles_with_trade_ledger():
    result = reconcile_order_fills(order(), [trade()])
    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["ledger_filled_qty"] == pytest.approx(0.4)
    assert result["weighted_avg_price"] == pytest.approx(100.0)
    assert result["commissions"]["USDT"] == pytest.approx(0.01)


def test_multiple_fills_weight_average_and_commission():
    second = trade(id="trade-2", price="110", qty="0.6", commission="0.02")
    result = reconcile_order_fills(
        order(executedQty="1", avgPrice="106", status="FILLED"),
        [trade(), second],
    )
    assert result["ok"] is True
    assert result["weighted_avg_price"] == pytest.approx(106.0)
    assert result["commissions"]["USDT"] == pytest.approx(0.03)


def test_fill_qty_mismatch_halts():
    result = reconcile_order_fills(order(executedQty="0.5"), [trade()])
    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert any("does not match" in reason for reason in result["reasons"])


def test_average_price_mismatch_halts():
    result = reconcile_order_fills(order(avgPrice="101"), [trade()])
    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_wrong_order_fill_halts():
    result = reconcile_order_fills(order(), [trade(orderId="different")])
    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_duplicate_fill_id_halts():
    result = reconcile_order_fills(order(executedQty="0.8"), [trade(), trade()])
    assert result["ok"] is False
    assert any("duplicate trade_id" in reason for reason in result["reasons"])


def test_filled_status_requires_full_quantity():
    result = reconcile_order_fills(
        order(status="FILLED", executedQty="0.4"), [trade()]
    )
    assert result["ok"] is False
    assert any("FILLED status" in reason for reason in result["reasons"])


def test_unknown_status_halts():
    result = reconcile_order_fills(order(status="UNKNOWN"), [trade()])
    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_empty_new_order_is_consistent():
    result = reconcile_order_fills(
        order(status="NEW", executedQty="0", avgPrice="0"), []
    )
    assert result["ok"] is True
    assert result["ledger_filled_qty"] == 0