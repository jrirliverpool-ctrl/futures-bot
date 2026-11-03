
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from position_reconciliation import reconcile_position_snapshot


SYMBOL = "BTC-SWAP-USDT"


def pos(**overrides):
    row = {
        "symbol": SYMBOL,
        "side": "LONG",
        "position": "2.5",
        "avgPrice": "60000",
        "available": "2.5",
    }
    row.update(overrides)
    return row


def expected(**overrides):
    data = {
        "side": "LONG",
        "quantity": "2.5",
        "avg_price": "60000",
    }
    data.update(overrides)
    return data


def test_flat_internal_matches_empty_exchange():
    result = reconcile_position_snapshot(SYMBOL, None, [])
    assert result["ok"] is True
    assert result["halt_trading"] is False


def test_flat_internal_rejects_unexpected_exchange_position():
    result = reconcile_position_snapshot(SYMBOL, None, [pos()])
    assert result["ok"] is False
    assert "unexpected_exchange_position" in result["reasons"]


def test_matching_long_position_passes():
    result = reconcile_position_snapshot(SYMBOL, expected(), [pos()])
    assert result["ok"] is True
    assert result["exchange_position"]["quantity"] == "2.5"


def test_matching_short_position_passes():
    row = pos(side="SHORT")
    result = reconcile_position_snapshot(
        SYMBOL, expected(side="SHORT"), [row]
    )
    assert result["ok"] is True


def test_missing_exchange_position_blocks():
    result = reconcile_position_snapshot(SYMBOL, expected(), [])
    assert "expected_position_missing_on_exchange" in result["reasons"]


def test_side_quantity_and_average_price_mismatches_block():
    cases = [
        (pos(side="SHORT"), expected(), "position_side_mismatch"),
        (pos(position="2.4"), expected(), "position_quantity_mismatch"),
        (pos(avgPrice="61000"), expected(), "position_average_price_mismatch"),
    ]

    for exchange, internal, reason in cases:
        result = reconcile_position_snapshot(
            SYMBOL, internal, [exchange]
        )
        assert result["ok"] is False
        assert reason in result["reasons"]


def test_duplicate_active_rows_block():
    result = reconcile_position_snapshot(
        SYMBOL, expected(), [pos(), pos()]
    )
    assert "multiple_active_positions_for_symbol" in result["reasons"]


def test_other_symbols_are_ignored():
    other = pos(symbol="ETH-SWAP-USDT")
    result = reconcile_position_snapshot(SYMBOL, None, [other])
    assert result["ok"] is True


def test_malformed_snapshot_fails_closed():
    for rows in (None, [None], [{"side": "LONG", "position": "1"}]):
        result = reconcile_position_snapshot(SYMBOL, None, rows)
        assert result["ok"] is False
        assert result["halt_trading"] is True


def test_nonfinite_and_negative_values_fail_closed():
    for row in (
        pos(position="NaN"),
        pos(position="-1"),
        pos(avgPrice="Infinity"),
    ):
        result = reconcile_position_snapshot(
            SYMBOL, expected(), [row]
        )
        assert result["ok"] is False
        assert result["halt_trading"] is True


def test_invalid_expected_state_fails_closed():
    for state in (
        "LONG",
        {},
        expected(quantity="0"),
        expected(avg_price="NaN"),
    ):
        result = reconcile_position_snapshot(SYMBOL, state, [])
        assert result["ok"] is False
        assert result["halt_trading"] is True


def test_tolerance_is_applied():
    result = reconcile_position_snapshot(
        SYMBOL,
        expected(quantity="2.500000001"),
        [pos()],
        qty_tolerance="0.00000001",
    )
    assert result["ok"] is True
