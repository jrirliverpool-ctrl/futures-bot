import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from toobit_open_algo_orders import (
    OpenAlgoOrdersReadError,
    fetch_all_open_algo_orders,
)


def row(order_id, category, side="LONG"):
    return {
        "orderId": str(order_id),
        "symbol": "BTC-SWAP-USDT",
        "positionSide": side,
        "type": "STOP_MARKET" if category == "STOP" else "STOP_PROFIT_LOSS_MARKET",
        "category": category,
    }


def test_fetches_both_categories_and_requires_two_matching_scans():
    calls = []
    def read(*, stop_category, symbol, order_id, limit):
        calls.append((stop_category, symbol, order_id, limit))
        if order_id is not None:
            return []
        if stop_category == "STOP":
            return [row(22, stop_category)]
        return [row(21, stop_category, "SHORT")]

    class Client:
        get_open_algo_orders_v2 = staticmethod(read)

    result = fetch_all_open_algo_orders(Client())

    assert [item["orderId"] for item in result] == ["22", "21"]
    assert len(calls) == 8  # two category scans, each run twice for stability
    assert {call[0] for call in calls} == {"STOP", "STOP_PROFIT_LOSS"}


def test_rejects_changing_snapshots():
    calls = {"n": 0}
    def read(*, stop_category, symbol, order_id, limit):
        calls["n"] += 1
        if order_id is not None:
            return []
        if stop_category == "STOP":
            return [row(22 if calls["n"] < 3 else 23, stop_category)]
        return []

    class Client:
        get_open_algo_orders_v2 = staticmethod(read)

    with pytest.raises(OpenAlgoOrdersReadError, match="snapshot_changed"):
        fetch_all_open_algo_orders(Client())


def test_missing_side_identity_fails_closed():
    class Client:
        @staticmethod
        def get_open_algo_orders_v2(**kwargs):
            if kwargs["stop_category"] == "STOP":
                return [{"orderId": "1", "symbol": "BTC-SWAP-USDT"}]
            return []

    with pytest.raises(OpenAlgoOrdersReadError, match="position_identity"):
        fetch_all_open_algo_orders(Client(), verify_stable=False)
