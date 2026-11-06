import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

from fill_ledger import (
    append_user_trades,
    load_fill_ledger,
    normalize_user_trade,
    reconcile_order_fills,
)


def v2_trade(**overrides):
    row = {
        "time": "1790000000123",
        "id": "v2-trade-1",
        "orderId": "v2-order-1",
        "symbol": "BTC-SWAP-USDT",
        "price": "100",
        "qty": "1",
        "commissionAsset": "USDT",
        "commission": "0.01",
        "makerRebate": "0",
        "type": "LIMIT",
        "side": "BUY",
        "positionSide": "LONG",
        "realizedPnl": "0",
        "ticketId": "ticket-v2-1",
        "isMaker": False,
    }
    row.update(overrides)
    return row


def v2_order(**overrides):
    row = {
        "orderId": "v2-order-1",
        "clientOrderId": "v2-client-1",
        "symbol": "BTC-SWAP-USDT",
        "side": "BUY",
        "positionSide": "LONG",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "100",
        "status": "FILLED",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "side,position_side,expected",
    [
        ("BUY", "LONG", "BUY_OPEN"),
        ("SELL", "SHORT", "SELL_OPEN"),
        ("SELL", "LONG", "SELL_CLOSE"),
        ("BUY", "SHORT", "BUY_CLOSE"),
    ],
)
def test_v2_side_and_position_side_are_normalized(
    side, position_side, expected
):
    row = normalize_user_trade(
        v2_trade(side=side, positionSide=position_side)
    )
    assert row["side"] == expected
    assert row["exchange_side"] == side
    assert row["position_side"] == position_side


def test_v2_negative_realized_pnl_is_preserved():
    row = normalize_user_trade(v2_trade(realizedPnl="-0.125"))
    assert row["realized_pnl"] == "-0.125"


def test_invalid_v2_side_combination_is_rejected():
    with pytest.raises(ValueError):
        normalize_user_trade(
            v2_trade(side="BUY", positionSide="BOTH")
        )


def test_v2_fill_reconciles_against_v2_order_snapshot():
    result = reconcile_order_fills(v2_order(), [v2_trade()])

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["ledger_filled_qty"] == pytest.approx(1.0)
    assert result["weighted_avg_price"] == pytest.approx(100.0)
    assert result["commissions"]["USDT"] == pytest.approx(0.01)


def test_v2_close_fill_reconciles_with_negative_realized_pnl():
    order = v2_order(side="SELL", positionSide="LONG")
    fill = v2_trade(
        side="SELL",
        positionSide="LONG",
        realizedPnl="-0.25",
    )
    result = reconcile_order_fills(order, [fill])

    assert result["ok"] is True
    assert result["realized_pnl"] == "-0.25"


def test_v2_ledger_append_is_idempotent(tmp_path):
    path = str(tmp_path / "v2-fills.jsonl")

    appended, skipped = append_user_trades(
        path, [v2_trade(), v2_trade()]
    )

    assert (appended, skipped) == (1, 1)
    rows = load_fill_ledger(path)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "v2-trade-1"
    assert rows[0]["side"] == "BUY_OPEN"
    assert rows[0]["exchange_side"] == "BUY"
    assert rows[0]["position_side"] == "LONG"


def test_same_trade_id_with_different_order_is_rejected(tmp_path):
    path = str(tmp_path / "v2-fills.jsonl")
    append_user_trades(path, [v2_trade()])

    with pytest.raises(ValueError, match="identity"):
        append_user_trades(
            path, [v2_trade(orderId="another-order")]
        )


def test_same_trade_id_with_different_symbol_is_rejected(tmp_path):
    path = str(tmp_path / "v2-fills.jsonl")
    append_user_trades(path, [v2_trade()])

    with pytest.raises(ValueError, match="identity"):
        append_user_trades(
            path, [v2_trade(symbol="ETH-SWAP-USDT")]
        )


def test_conflicting_duplicate_v2_fill_is_rejected(tmp_path):
    path = str(tmp_path / "v2-fills.jsonl")
    append_user_trades(path, [v2_trade()])

    with pytest.raises(ValueError, match="conflicting"):
        append_user_trades(path, [v2_trade(price="101")])


def test_legacy_ledger_rows_remain_readable(tmp_path):
    path = tmp_path / "legacy-fills.jsonl"
    path.write_text(
        '{"trade_id":"old-1","order_id":"old-order",'
        '"symbol":"BTC-SWAP-USDT","timestamp_ms":1790000000123,'
        '"side":"BUY_OPEN","qty":"1","price":"100",'
        '"commission":"0.01","commission_asset":"USDT",'
        '"maker_rebate":"0","realized_pnl":"0",'
        '"is_maker":false,"ticket_id":null,"trade_type":"LIMIT"}\n',
        encoding="utf-8",
    )

    rows = load_fill_ledger(str(path))
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY_OPEN"
    assert rows[0]["exchange_side"] is None