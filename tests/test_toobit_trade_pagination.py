
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "scripts"),
)

from toobit_trade_pagination import (
    TradePaginationError,
    fetch_complete_user_trades_v2,
)


SYMBOL = "BTC-SWAP-USDT"


def trade(trade_id, **overrides):
    row = {
        "id": str(trade_id),
        "symbol": SYMBOL,
        "orderId": "order-1",
        "price": "100",
        "qty": "1",
        "commission": "0.01",
        "commissionAsset": "USDT",
        "side": "BUY",
        "positionSide": "LONG",
        "time": 1790000000000,
    }
    row.update(overrides)
    return row


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_user_trades_v2(
        self,
        symbol,
        start_time=None,
        end_time=None,
        limit=1000,
        from_id=None,
        to_id=None,
    ):
        self.calls.append({
            "symbol": symbol,
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
            "from_id": from_id,
            "to_id": to_id,
        })

        if not self.pages:
            raise AssertionError(
                "unexpected extra page request"
            )

        result = self.pages.pop(0)

        if isinstance(result, Exception):
            raise result

        return result


def test_empty_first_page_returns_empty_history():
    client = FakeClient([[]])

    result = fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        page_size=2,
    )

    assert result == []
    assert len(client.calls) == 1
    assert client.calls[0]["from_id"] is None


def test_short_page_proves_completion():
    rows = [trade(10), trade(9)]
    client = FakeClient([rows])

    result = fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        page_size=3,
    )

    assert result == rows
    assert len(client.calls) == 1


def test_full_page_then_empty_page_completes():
    first = [trade(10), trade(9)]
    client = FakeClient([first, []])

    result = fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        page_size=2,
    )

    assert [row["id"] for row in result] == ["10", "9"]
    assert len(client.calls) == 2
    assert [
        call["from_id"] for call in client.calls
    ] == [None, 9]


def test_multiple_pages_preserve_descending_order():
    client = FakeClient([
        [trade(14), trade(13)],
        [trade(12), trade(11)],
        [trade(10)],
    ])

    result = fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        page_size=2,
    )

    assert [
        int(row["id"]) for row in result
    ] == [14, 13, 12, 11, 10]

    assert [
        call["from_id"] for call in client.calls
    ] == [None, 13, 11]


def test_initial_from_id_is_forwarded():
    client = FakeClient([
        [trade(99), trade(98)],
        [],
    ])

    fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        page_size=2,
        initial_from_id=100,
    )

    assert client.calls[0]["from_id"] == 100


def test_time_window_is_forwarded_to_every_page():
    client = FakeClient([
        [trade(10), trade(9)],
        [],
    ])

    fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        start_time=1000,
        end_time=2000,
        page_size=2,
    )

    assert all(
        call["start_time"] == 1000
        and call["end_time"] == 2000
        for call in client.calls
    )


@pytest.mark.parametrize(
    "bad_id",
    [None, "", "abc", "-1", "1.5", "NaN", "Infinity"],
)
def test_invalid_trade_id_fails_closed(bad_id):
    client = FakeClient([[trade(bad_id)]])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_boolean_trade_id_is_rejected():
    client = FakeClient([[trade(True)]])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_duplicate_ids_fail_closed():
    client = FakeClient([
        [trade(10), trade(10)],
    ])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_ascending_ids_fail_closed():
    client = FakeClient([
        [trade(9), trade(10)],
    ])

    with pytest.raises(
        TradePaginationError,
        match="trade_ids_not_strictly_decreasing",
    ):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_duplicate_across_pages_fails_closed():
    client = FakeClient([
        [trade(10), trade(9)],
        [trade(9)],
    ])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_page_must_be_below_from_id_cursor():
    client = FakeClient([
        [trade(10), trade(9)],
        [trade(9)],
    ])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
            initial_from_id=11,
        )


def test_symbol_mismatch_fails_closed():
    client = FakeClient([
        [trade(10, symbol="ETH-SWAP-USDT")],
    ])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


@pytest.mark.parametrize(
    "bad_page",
    [
        None,
        {},
        "invalid",
        [trade(10), "invalid-row"],
    ],
)
def test_malformed_page_fails_closed(bad_page):
    client = FakeClient([bad_page])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_page_larger_than_requested_limit_fails():
    client = FakeClient([
        [trade(10), trade(9), trade(8)],
    ])

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


def test_page_limit_exhaustion_does_not_return_partial_history():
    client = FakeClient([
        [trade(10), trade(9)],
        [trade(8), trade(7)],
    ])

    with pytest.raises(
        TradePaginationError,
        match="pagination_page_limit_reached",
    ):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
            max_pages=2,
        )


def test_exchange_exception_propagates():
    client = FakeClient([
        TimeoutError("network timeout"),
    ])

    with pytest.raises(TimeoutError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            page_size=2,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": 0},
        {"page_size": 1001},
        {"page_size": True},
        {"max_pages": 0},
        {"max_pages": True},
        {"initial_from_id": -1},
        {"initial_from_id": True},
    ],
)
def test_invalid_pagination_configuration_is_rejected(kwargs):
    client = FakeClient([])

    with pytest.raises(ValueError):
        fetch_complete_user_trades_v2(
            client,
            SYMBOL,
            **kwargs,
        )


def test_client_without_v2_method_is_rejected():
    class EmptyClient:
        pass

    with pytest.raises(TradePaginationError):
        fetch_complete_user_trades_v2(
            EmptyClient(),
            SYMBOL,
        )


def test_zero_id_page_proves_oldest_boundary():
    client = FakeClient([
        [trade(2), trade(0)],
    ])

    result = fetch_complete_user_trades_v2(
        client,
        SYMBOL,
        page_size=2,
    )

    assert [row["id"] for row in result] == ["2", "0"]
    assert len(client.calls) == 1
