import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from toobit_open_orders import (
    fetch_all_open_orders,
    OpenOrdersReadError,
)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _request(self, method, path, params):
        self.calls.append((method, path, dict(params)))

        cursor = params.get("orderId")
        key = None if cursor is None else int(cursor)

        if key not in self.pages:
            return []

        return self.pages[key]


def row(order_id, **extra):
    result = {
        "orderId": str(order_id),
        "symbol": "BTC-SWAP-USDT",
        "type": "STOP_PROFIT_LOSS",
        "status": "ORDER_NEW",
    }
    result.update(extra)
    return result


def test_paginates_until_empty_and_repeats_stable_scan():
    client = FakeClient({
        None: [row(103), row(102)],
        102: [row(101), row(100)],
        100: [],
    })

    result = fetch_all_open_orders(client)

    assert [int(item["orderId"]) for item in result] == [
        103, 102, 101, 100
    ]

    # Two complete scans: 3 requests per scan.
    assert len(client.calls) == 6
    assert all(call[0] == "GET" for call in client.calls)


def test_empty_account_returns_empty_after_two_scans():
    client = FakeClient({None: []})

    assert fetch_all_open_orders(client) == []
    assert len(client.calls) == 2


def test_cursor_that_does_not_move_backwards_fails():
    client = FakeClient({
        None: [row(103), row(102)],
        102: [row(103)],
    })

    with pytest.raises(OpenOrdersReadError):
        fetch_all_open_orders(client, verify_stable=False)


def test_duplicate_order_id_fails():
    client = FakeClient({
        None: [row(103), row(102)],
        102: [row(102)],
    })

    with pytest.raises(OpenOrdersReadError):
        fetch_all_open_orders(client, verify_stable=False)


def test_missing_order_id_fails():
    client = FakeClient({
        None: [{
            "symbol": "BTC-SWAP-USDT",
            "type": "STOP_PROFIT_LOSS",
        }],
    })

    with pytest.raises(OpenOrdersReadError):
        fetch_all_open_orders(client, verify_stable=False)


def test_transport_unknown_fails_closed():
    class BrokenClient:
        def _request(self, method, path, params):
            return {"_transport": "UNKNOWN"}

    with pytest.raises(OpenOrdersReadError):
        fetch_all_open_orders(BrokenClient())


def test_non_list_response_fails_closed():
    class BrokenClient:
        def _request(self, method, path, params):
            return {"code": -1}

    with pytest.raises(OpenOrdersReadError):
        fetch_all_open_orders(BrokenClient())


def test_page_limit_exhaustion_fails_closed():
    client = FakeClient({
        None: [row(103)],
        103: [row(102)],
    })

    with pytest.raises(OpenOrdersReadError):
        fetch_all_open_orders(
            client,
            max_pages=1,
            verify_stable=False,
        )


def test_invalid_arguments_are_rejected():
    client = FakeClient({None: []})

    with pytest.raises(ValueError):
        fetch_all_open_orders(client, limit=1001)

    with pytest.raises(ValueError):
        fetch_all_open_orders(client, max_pages=0)

    with pytest.raises(ValueError):
        fetch_all_open_orders(client, order_type="INVALID")