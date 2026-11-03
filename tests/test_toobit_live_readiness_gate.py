
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from toobit_live_readiness_gate import evaluate_readiness


def snapshot(**overrides):
    data = {
        "ok": True,
        "read_only": True,
        "halt_live": False,
        "blockers": [],
        "wallet": {
            "coin": "USDT",
            "availableBalance": "25.00",
        },
        "positions": [],
        "open_orders": [],
        "symbols_checked": [
            "BTC-SWAP-USDT",
            "ETH-SWAP-USDT",
        ],
        "leverage_settings": [
            {"symbolId": "BTC-SWAP-USDT"},
            {"symbolId": "ETH-SWAP-USDT"},
        ],
    }
    data.update(overrides)
    return data


def test_positive_available_balance_passes_to_manual_review_only():
    result = evaluate_readiness(snapshot())

    assert result["ok"] is True
    assert result["ready_for_manual_review"] is True
    assert result["manual_authorization_required"] is True
    assert result["orders_submitted"] == 0


def test_zero_available_balance_blocks():
    data = snapshot()
    data["wallet"]["availableBalance"] = "0"

    result = evaluate_readiness(data)

    assert result["ok"] is False
    assert "no_usable_available_usdt_balance" in result["blockers"]


def test_missing_available_balance_blocks():
    data = snapshot()
    data["wallet"].pop("availableBalance")

    result = evaluate_readiness(data)

    assert "no_usable_available_usdt_balance" in result["blockers"]


def test_negative_or_nonfinite_balance_blocks():
    for value in ("-1", "NaN", "Infinity", True, None):
        data = snapshot()
        data["wallet"]["availableBalance"] = value

        assert evaluate_readiness(data)["ok"] is False


def test_existing_base_blockers_are_preserved():
    data = snapshot()
    data["blockers"] = ["open_positions_exist"]
    data["halt_live"] = True

    result = evaluate_readiness(data)

    assert result["ok"] is False
    assert "base:open_positions_exist" in result["blockers"]
    assert "base_preflight_halt_live" in result["blockers"]


def test_any_position_or_order_blocks():
    assert evaluate_readiness(
        snapshot(positions=[{"position": "1"}])
    )["ok"] is False

    assert evaluate_readiness(
        snapshot(open_orders=[{"orderId": "1"}])
    )["ok"] is False


def test_incomplete_leverage_coverage_blocks():
    data = snapshot()
    data["leverage_settings"] = data["leverage_settings"][:1]

    result = evaluate_readiness(data)

    assert "leverage_coverage_incomplete" in result["blockers"]


def test_malformed_snapshot_fails_closed():
    result = evaluate_readiness(None)

    assert result["ok"] is False
    assert result["read_only"] is True
    assert result["orders_submitted"] == 0
