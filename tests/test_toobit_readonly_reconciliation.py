
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

from toobit_readonly_reconciliation import (
    ToobitReadOnlyReconciliation,
)


SYMBOL = "BTC-SWAP-USDT"


def position(**overrides):
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


class FakeStore:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error
        self.calls = []

    def recovery_required(self):
        self.calls.append("recovery_required")

        if self.error:
            raise self.error

        return self.records


class FakeClient:
    def __init__(self, positions):
        self.positions = positions
        self.calls = []

    def _request(self, method, path, params):
        self.calls.append((method, path, params))

        if method != "GET":
            raise AssertionError("Only GET is permitted")

        if path != "/api/v1/futures/positions":
            raise AssertionError("Unexpected endpoint")

        return self.positions

    def place_order(self, **kwargs):
        raise AssertionError("Order submission is forbidden")

    def cancel_order(self, **kwargs):
        raise AssertionError("Order cancellation is forbidden")


def coordinator(client=None, store=None, expected_state=None):
    client = client or FakeClient([position()])
    store = store or FakeStore()

    if expected_state is None:
        expected_state = {SYMBOL: expected()}

    return ToobitReadOnlyReconciliation(
        client,
        store,
        [SYMBOL],
        expected_state,
    )


def test_matching_account_snapshot_passes_read_only():
    client = FakeClient([position()])
    store = FakeStore()

    result = coordinator(client, store).run(
        now_ms=1790000005000
    )

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["positions_ok"] is True
    assert result["orders_ok"] is True
    assert result["read_only"] is True

    assert client.calls == [
        ("GET", "/api/v1/futures/positions", {})
    ]

    assert store.calls == ["recovery_required"]

    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0
    assert result["journal_mutated"] is False
    assert result["trading_state_mutated"] is False


def test_matching_hedge_mode_snapshot_passes_for_both_sides():
    long_position = position(side="LONG", position="2.5")
    short_position = position(side="SHORT", position="1.25", avgPrice="61000")
    expected_both = {
        SYMBOL: {
            "LONG": {"quantity": "2.5", "avg_price": "60000"},
            "SHORT": {"quantity": "1.25", "avg_price": "61000"},
        }
    }
    client = FakeClient([long_position, short_position])
    result = coordinator(
        client=client,
        store=FakeStore(),
        expected_state=expected_both,
    ).run(now_ms=1790000005000)
    assert result["positions_ok"] is True
    assert result["halt_trading"] is False
    assert set(result["position_reports"][SYMBOL]["exchange_position"]) == {
        "LONG", "SHORT"
    }


def test_duplicate_same_side_snapshot_halts_in_hedge_mode():
    client = FakeClient([position(), position(position="1")])
    expected_both = {
        SYMBOL: {
            "LONG": {"quantity": "2.5", "avg_price": "60000"},
            "SHORT": {"quantity": "1", "avg_price": "61000"},
        }
    }
    result = coordinator(
        client=client,
        store=FakeStore(),
        expected_state=expected_both,
    ).run(now_ms=1790000005000)
    assert result["halt_trading"] is True
    assert result["positions_ok"] is False


def test_empty_exchange_snapshot_matches_flat_internal_state():
    client = FakeClient([])

    result = coordinator(
        client,
        FakeStore(),
        {SYMBOL: None},
    ).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["halt_trading"] is False


def test_quantity_mismatch_halts():
    client = FakeClient([
        position(position="2.4")
    ])

    result = coordinator(client).run(
        now_ms=1790000005000
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["positions_ok"] is False


def test_side_mismatch_halts():
    client = FakeClient([
        position(side="SHORT")
    ])

    result = coordinator(client).run(
        now_ms=1790000005000
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_unexpected_active_symbol_halts():
    client = FakeClient([
        position(),
        {
            "symbol": "ETH-SWAP-USDT",
            "side": "LONG",
            "position": "1",
            "avgPrice": "3000",
        },
    ])

    result = coordinator(client).run(
        now_ms=1790000005000
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["reason"] == (
        "active_positions_outside_configured_universe"
    )


def test_unexpected_flat_symbol_does_not_block():
    client = FakeClient([
        position(),
        {
            "symbol": "ETH-SWAP-USDT",
            "side": "LONG",
            "position": "0",
            "avgPrice": "0",
        },
    ])

    result = coordinator(client).run(
        now_ms=1790000005000
    )

    assert result["ok"] is True


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        "invalid",
        [None],
    ],
)
def test_incomplete_positions_response_halts(response):
    client = FakeClient(response)

    result = coordinator(client).run(
        now_ms=1790000005000
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True


def test_positions_transport_exception_halts():
    class BrokenClient:
        def _request(self, method, path, params):
            raise TimeoutError("simulated timeout")

    result = coordinator(
        BrokenClient(),
        FakeStore(),
    ).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["reason"] == "positions_request_failed"


def test_corrupt_intent_store_halts():
    client = FakeClient([position()])
    store = FakeStore(error=ValueError("corrupt journal"))

    result = coordinator(client, store).run(
        now_ms=1790000005000
    )

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["orders_ok"] is False


def test_expected_symbols_must_match_configured_symbols():
    with pytest.raises(ValueError):
        ToobitReadOnlyReconciliation(
            FakeClient([]),
            FakeStore(),
            [SYMBOL],
            {},
        )


def test_duplicate_symbols_are_rejected():
    with pytest.raises(ValueError):
        ToobitReadOnlyReconciliation(
            FakeClient([]),
            FakeStore(),
            [SYMBOL, SYMBOL],
            {SYMBOL: None},
        )


def test_invalid_symbol_is_rejected():
    with pytest.raises(ValueError):
        ToobitReadOnlyReconciliation(
            FakeClient([]),
            FakeStore(),
            ["BTCUSDT"],
            {"BTCUSDT": None},
        )
