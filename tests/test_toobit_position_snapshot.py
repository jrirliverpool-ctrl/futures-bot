
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from toobit_position_snapshot import (
    PositionSnapshotError,
    fetch_stable_position_snapshot,
)


SYMBOL = "BTC-SWAP-USDT"


def position(**overrides):
    row = {
        "symbol": SYMBOL,
        "side": "LONG",
        "position": "2.5",
        "available": "2.5",
        "avgPrice": "60000",
        "leverage": "2",
        "marginType": "ISOLATED",
        "markPrice": "60100",
        "unrealizedPnL": "1",
    }
    row.update(overrides)
    return row


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, path, params):
        self.calls.append((method, path, params))

        if method != "GET":
            raise AssertionError("Only GET is permitted")

        if path != "/api/v1/futures/positions":
            raise AssertionError("Unexpected endpoint")

        if not self.responses:
            raise AssertionError("Unexpected extra request")

        response = self.responses.pop(0)

        if isinstance(response, Exception):
            raise response

        return response


def test_two_identical_position_snapshots_pass():
    first = [position()]
    second = [position()]

    client = FakeClient([first, second])

    result = fetch_stable_position_snapshot(client)

    assert result == second
    assert len(client.calls) == 2
    assert all(call[0] == "GET" for call in client.calls)


def test_available_quantity_change_makes_snapshot_unstable():
    client = FakeClient([
        [position(available="2.5")],
        [position(available="2.0")],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="positions_snapshot_changed_between_reads",
    ):
        fetch_stable_position_snapshot(client)


def test_total_position_change_makes_snapshot_unstable():
    client = FakeClient([
        [
            position(
                position="2.5",
                available="2.5",
            )
        ],
        [
            position(
                position="2.0",
                available="2.0",
            )
        ],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="positions_snapshot_changed_between_reads",
    ):
        fetch_stable_position_snapshot(client)


def test_average_entry_price_change_makes_snapshot_unstable():
    client = FakeClient([
        [position(avgPrice="60000")],
        [position(avgPrice="60100")],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="positions_snapshot_changed_between_reads",
    ):
        fetch_stable_position_snapshot(client)


def test_volatile_mark_price_change_does_not_break_snapshot():
    client = FakeClient([
        [position(markPrice="60000", unrealizedPnL="1")],
        [position(markPrice="61000", unrealizedPnL="5")],
    ])

    result = fetch_stable_position_snapshot(client)

    assert result[0]["markPrice"] == "61000"
    assert len(client.calls) == 2


def test_active_position_requires_available_quantity():
    client = FakeClient([
        [position(available=None)],
        [position(available=None)],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="position_available_missing",
    ):
        fetch_stable_position_snapshot(client)


@pytest.mark.parametrize(
    "bad_available",
    [
        "-0.1",
        "NaN",
        "Infinity",
        "not-a-number",
        "3",
    ],
)
def test_invalid_available_quantity_fails_closed(bad_available):
    client = FakeClient([
        [position(available=bad_available)],
        [position(available=bad_available)],
    ])

    with pytest.raises(PositionSnapshotError):
        fetch_stable_position_snapshot(client)


def test_zero_available_is_valid_but_must_be_stable():
    client = FakeClient([
        [position(available="0")],
        [position(available="0")],
    ])

    result = fetch_stable_position_snapshot(client)

    assert result[0]["available"] == "0"


def test_flat_position_can_omit_available():
    flat = {
        "symbol": SYMBOL,
        "side": "LONG",
        "position": "0",
        "avgPrice": "0",
    }

    client = FakeClient([
        [flat],
        [dict(flat)],
    ])

    result = fetch_stable_position_snapshot(client)

    assert result == [dict(flat)]


def test_flat_position_cannot_have_nonzero_available():
    flat = {
        "symbol": SYMBOL,
        "side": "LONG",
        "position": "0",
        "available": "1",
    }

    client = FakeClient([
        [flat],
        [dict(flat)],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="flat_position_has_nonzero_available",
    ):
        fetch_stable_position_snapshot(client)


def test_duplicate_position_side_fails_closed():
    client = FakeClient([
        [position(), position()],
        [position(), position()],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="duplicate_position_side",
    ):
        fetch_stable_position_snapshot(client)


def test_invalid_position_quantity_fails_closed():
    client = FakeClient([
        [position(position="NaN")],
        [position(position="NaN")],
    ])

    with pytest.raises(PositionSnapshotError):
        fetch_stable_position_snapshot(client)


def test_invalid_average_price_fails_closed():
    client = FakeClient([
        [position(avgPrice="0")],
        [position(avgPrice="0")],
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="position_average_price_not_positive",
    ):
        fetch_stable_position_snapshot(client)


def test_request_error_is_wrapped_without_exposing_response():
    client = FakeClient([
        RuntimeError("private response text"),
    ])

    with pytest.raises(
        PositionSnapshotError,
        match="positions_request_failed:RuntimeError",
    ) as exc:
        fetch_stable_position_snapshot(client)

    assert "private response text" not in str(exc.value)


def test_invalid_client_fails_closed():
    with pytest.raises(
        PositionSnapshotError,
        match="positions_client_missing_request_method",
    ):
        fetch_stable_position_snapshot(object())
