import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from toobit_commission_rates import (
    CommissionRateError,
    fetch_stable_commission_rates,
)


def fee_payload(open_taker="0.0002"):
    return {
        "openMakerFee": "0.0001",
        "openTakerFee": open_taker,
        "closeMakerFee": "0.0001",
        "closeTakerFee": "0.0003",
    }


def test_reads_current_rates_twice_and_calculates_taker_round_trip():
    class Client:
        calls = []

        def _request(self, method, path, params):
            self.calls.append((method, path, params))
            return fee_payload()

    client = Client()
    result = fetch_stable_commission_rates(client, "BTC-SWAP-USDT")

    assert len(client.calls) == 2
    assert all(call[0] == "GET" for call in client.calls)
    assert result["symbol"] == "BTC-SWAP-USDT"
    assert result["round_trip_taker_rate"] == "0.0005"
    assert result["stable_snapshot_verified"] is True


def test_rejects_changing_rate_snapshot():
    class Client:
        calls = 0

        def _request(self, method, path, params):
            self.calls += 1
            return fee_payload("0.0002" if self.calls == 1 else "0.0004")

    with pytest.raises(CommissionRateError, match="changed_between_reads"):
        fetch_stable_commission_rates(Client(), "BTC-SWAP-USDT")


@pytest.mark.parametrize(
    "payload",
    [
        {"openMakerFee": "0.0001"},
        {**fee_payload(), "closeTakerFee": float("nan")},
        {**fee_payload(), "closeMakerFee": -0.001},
        {"_transport": "UNKNOWN"},
    ],
)
def test_rejects_incomplete_or_invalid_fee_payload(payload):
    class Client:
        def _request(self, method, path, params):
            return payload

    with pytest.raises(CommissionRateError):
        fetch_stable_commission_rates(Client(), "BTC-SWAP-USDT")
