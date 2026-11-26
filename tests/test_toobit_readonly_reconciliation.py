
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from toobit_readonly_reconciliation import ToobitReadOnlyReconciliation


SYMBOL = "BTC-SWAP-USDT"
ALGO_ENDPOINT = "/api/v2/futures/open-algo-orders"


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


def protective_stop(**overrides):
    row = {
        "orderId": "7001",
        "clientOrderId": "sl-7001",
        "symbol": SYMBOL,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_PROFIT_LOSS_MARKET",
        "status": "ORDER_NEW",
        "origQty": "2.5",
        "executedQty": "0",
        "stopPrice": "57000",
        "stopOrderType": "STOP_LOSS",
        "stopType": "FIXED_STOP",
    }
    row.update(overrides)
    return row


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
    def __init__(
        self,
        positions,
        stop_orders=None,
        stop_category_orders=None,
        unstable_algo=False,
    ):
        self.positions = positions
        self.stop_orders = (
            [protective_stop()] if stop_orders is None else stop_orders
        )
        self.stop_category_orders = (
            [] if stop_category_orders is None else stop_category_orders
        )
        self.unstable_algo = unstable_algo
        self.calls = []
        self.algo_reads = 0

    def _request(self, method, path, params):
        self.calls.append((method, path, params))
        if method != "GET":
            raise AssertionError("Only GET is permitted")
        if path != "/api/v1/futures/positions":
            raise AssertionError("Unexpected V1 endpoint")
        return self.positions

    def _request_v2_get(self, path, params):
        self.calls.append(("GET", path, dict(params)))
        if path != ALGO_ENDPOINT:
            raise AssertionError("Unexpected V2 endpoint")

        category = params.get("stopCategory")
        self.algo_reads += 1

        if category == "STOP":
            return self.stop_category_orders

        if category == "STOP_PROFIT_LOSS":
            rows = self.stop_orders
            if self.unstable_algo and self.algo_reads >= 4:
                rows = [
                    dict(row, stopPrice="56000")
                    for row in rows
                ]
            return rows

        raise AssertionError("Unexpected algo category")

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
    result = coordinator(client, store).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["positions_ok"] is True
    assert result["protection_ok"] is True
    assert result["orders_ok"] is True
    assert result["read_only"] is True

    position_calls = [
        call for call in client.calls
        if call[1] == "/api/v1/futures/positions"
    ]
    algo_calls = [
        call for call in client.calls
        if call[1] == ALGO_ENDPOINT
    ]
    assert len(position_calls) == 2
    assert len(algo_calls) == 4
    assert all(call[0] == "GET" for call in client.calls)
    assert store.calls == ["recovery_required"]

    assert result["orders_submitted"] == 0
    assert result["orders_amended"] == 0
    assert result["orders_canceled"] == 0
    assert result["journal_mutated"] is False
    assert result["trading_state_mutated"] is False


def test_flat_exchange_snapshot_matches_flat_internal_state():
    client = FakeClient([], stop_orders=[])
    result = coordinator(
        client,
        FakeStore(),
        {SYMBOL: None},
    ).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["halt_trading"] is False
    assert result["protection_ok"] is True


def test_missing_protective_stop_loss_halts():
    client = FakeClient([position()], stop_orders=[])
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["protection_ok"] is False
    assert result["protective_orders_report"]["unprotected_positions"] == 1


def test_take_profit_only_does_not_count_as_protection():
    tp = protective_stop(stopOrderType="TAKE_PROFIT")
    client = FakeClient([position()], stop_orders=[tp])
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["protection_ok"] is False


def test_insufficient_stop_quantity_halts():
    stop = protective_stop(origQty="2.0", executedQty="0")
    client = FakeClient([position()], stop_orders=[stop])
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["protection_ok"] is False


def test_short_position_requires_buy_stop_loss():
    short_position = position(side="SHORT")
    short_expected = {
        "side": "SHORT",
        "quantity": "2.5",
        "avg_price": "60000",
    }
    stop = protective_stop(
        side="BUY",
        positionSide="SHORT",
    )
    client = FakeClient([short_position], stop_orders=[stop])
    result = coordinator(
        client,
        FakeStore(),
        {SYMBOL: short_expected},
    ).run(now_ms=1790000005000)

    assert result["ok"] is True
    assert result["protection_ok"] is True


def test_side_mismatch_halts():
    client = FakeClient([position(side="SHORT")])
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["positions_ok"] is False


def test_quantity_mismatch_halts():
    client = FakeClient([position(position="2.4")])
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["positions_ok"] is False


def test_unexpected_active_symbol_halts_before_algo_scan():
    client = FakeClient([
        position(),
        {
            "symbol": "ETH-SWAP-USDT",
            "side": "LONG",
            "position": "1",
            "available": "1",
            "avgPrice": "3000",
        },
    ])
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["reason"] == "active_positions_outside_configured_universe"
    assert not any(call[1] == ALGO_ENDPOINT for call in client.calls)


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
    result = coordinator(client).run(now_ms=1790000005000)
    assert result["ok"] is True


@pytest.mark.parametrize("response", [None, {}, "invalid", [None]])
def test_incomplete_positions_response_halts(response):
    client = FakeClient(response)
    result = coordinator(client).run(now_ms=1790000005000)
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
    assert result["reason"] == "positions_snapshot_not_stable"
    assert result["detail"] == "positions_request_failed:TimeoutError"


def test_algo_snapshot_transport_exception_halts():
    class BrokenAlgoClient(FakeClient):
        def _request_v2_get(self, path, params):
            raise TimeoutError("simulated algo timeout")

    result = coordinator(BrokenAlgoClient([position()])).run(
        now_ms=1790000005000
    )
    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["reason"] == "algo_orders_snapshot_not_stable"
    assert result["protection_ok"] is False


def test_unstable_algo_snapshot_halts():
    client = FakeClient([position()], unstable_algo=True)
    result = coordinator(client).run(now_ms=1790000005000)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["reason"] == "algo_orders_snapshot_not_stable"


def test_corrupt_intent_store_halts():
    client = FakeClient([position()])
    store = FakeStore(error=ValueError("corrupt journal"))
    result = coordinator(client, store).run(now_ms=1790000005000)

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
