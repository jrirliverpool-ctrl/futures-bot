
"""Regression tests for execution Decision propagation across V1/V2."""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import paper_engine as v1
import paper_engine_v2 as v2
from state_manager_v2 import default_state_v2


BTC = "BTC-SWAP-USDT"


class Decision:
    def __init__(self, accepted, reason=None, gate=None):
        self.accepted = accepted
        self.reason = reason
        self.gate = gate


def valid_specs():
    return {
        "contract_multiplier": 1.0,
        "min_qty": 0.001,
        "max_qty": 100000.0,
        "qty_step": 0.001,
        "min_notional": 0.0,
        "max_leverage": 20,
    }


def valid_config():
    return {
        "risk_per_trade": 0.001,
        "sl_atr_mult": 1.5,
        "open_taker_fee": 0.0,
        "close_taker_fee": 0.0,
        "slippage_pct": 0.0,
    }


def valid_entry_info():
    return {
        "intent": "ENTRY",
        "side": "LONG",
        "qty": 1.0,
        "price": 100.0,
        "sl": 90.0,
        "tp1": 120.0,
        "leverage": 1,
        "timestamp": 1000,
        "signal_candle_ts": 900,
    }


def make_state():
    return default_state_v2(
        initial_equity=1000.0,
        symbols=[BTC],
        max_concurrent_positions=1,
        max_portfolio_risk=0.01,
    )


@pytest.mark.parametrize("accepted", [True, False])
def test_v1_callback_decision_parser_preserves_fields(accepted):
    decision = Decision(
        accepted=accepted,
        reason="risk_limit_exceeded",
        gate="portfolio_risk",
    )

    parsed = v1._callback_decision(decision)

    assert parsed == (
        accepted,
        "risk_limit_exceeded",
        "portfolio_risk",
    )


def test_v1_callback_rejection_fields_preserve_reason_and_gate():
    decision = Decision(
        accepted=False,
        reason="symbol_not_verified",
        gate="symbol_specs",
    )

    rejection = v1._callback_rejection_fields(decision)

    assert rejection == {
        "reason": "symbol_not_verified",
        "gate": "symbol_specs",
    }


def test_v1_unknown_callback_result_fails_closed():
    accepted, reason, gate = v1._callback_decision(object())

    assert accepted is False
    assert reason == "callback_result_invalid"
    assert gate == "callback"


@pytest.mark.parametrize("accepted", [True, False])
def test_v2_bridge_preserves_decision_object(monkeypatch, accepted):
    """
    A realistic ENTRY callback must preserve the exact Decision object.

    The V2 portfolio capacity and risk gates are still active. The
    test uses valid entry fields and a portfolio limit that allows
    the proposed entry, so the external Decision reaches V1 unchanged.
    """

    state = make_state()

    decision = Decision(
        accepted=accepted,
        reason="test_decision",
        gate="test_gate",
    )

    captured = {}

    def fake_v1_process_candle(
        state_v1,
        klines,
        indicators,
        index,
        specs,
        config,
        signal=None,
        execution_callback=None,
        exit_callback=None,
    ):
        captured["callback_result"] = execution_callback(
            valid_entry_info(),
            state_v1,
        )
        return state_v1, []

    monkeypatch.setattr(
        v2,
        "_v1_process_candle",
        fake_v1_process_candle,
    )

    result_state, events = v2.process_symbol_candle(
        state_v2=state,
        symbol=BTC,
        klines=[],
        indicators={},
        i=0,
        specs=valid_specs(),
        config=valid_config(),
        execution_callback=lambda info, state_ref: decision,
    )

    assert result_state is state
    assert events == []

    assert captured["callback_result"] is decision
    assert captured["callback_result"].accepted is accepted
    assert captured["callback_result"].reason == "test_decision"
    assert captured["callback_result"].gate == "test_gate"


def test_v2_risk_gate_still_rejects_entry_above_portfolio_limit(
    monkeypatch,
):
    """
    An external approval must not bypass the V2 portfolio risk limit.
    """

    state = make_state()
    captured = {}

    decision = Decision(
        accepted=True,
        reason="external_approval",
        gate="external",
    )

    excessive_entry = valid_entry_info()
    excessive_entry["qty"] = 1.01

    def fake_v1_process_candle(
        state_v1,
        klines,
        indicators,
        index,
        specs,
        config,
        signal=None,
        execution_callback=None,
        exit_callback=None,
    ):
        captured["callback_result"] = execution_callback(
            excessive_entry,
            state_v1,
        )
        return state_v1, []

    monkeypatch.setattr(
        v2,
        "_v1_process_candle",
        fake_v1_process_candle,
    )

    result_state, events = v2.process_symbol_candle(
        state_v2=state,
        symbol=BTC,
        klines=[],
        indicators={},
        i=0,
        specs=valid_specs(),
        config=valid_config(),
        execution_callback=lambda info, state_ref: decision,
    )

    assert result_state is state
    assert events == []

    rejection = captured["callback_result"]

    assert isinstance(rejection, dict)
    assert rejection["accepted"] is False
    assert rejection["gate"] == "portfolio_risk"
    assert rejection["reason"] == "portfolio_risk_limit_exceeded"


def test_v2_invalid_entry_without_external_callback_fails_closed(
    monkeypatch,
):
    """
    Missing actual entry fields must never be interpreted as zero risk.
    """

    state = make_state()
    captured = {}

    def fake_v1_process_candle(
        state_v1,
        klines,
        indicators,
        index,
        specs,
        config,
        signal=None,
        execution_callback=None,
        exit_callback=None,
    ):
        captured["callback_result"] = execution_callback(
            {"intent": "ENTRY"},
            state_v1,
        )
        return state_v1, []

    monkeypatch.setattr(
        v2,
        "_v1_process_candle",
        fake_v1_process_candle,
    )

    result_state, events = v2.process_symbol_candle(
        state_v2=state,
        symbol=BTC,
        klines=[],
        indicators={},
        i=0,
        specs=valid_specs(),
        config=valid_config(),
        execution_callback=None,
    )

    assert result_state is state
    assert events == []

    rejection = captured["callback_result"]

    assert isinstance(rejection, dict)
    assert rejection["accepted"] is False
    assert rejection["gate"] == "portfolio_risk"
    assert rejection["reason"] == "new_entry_risk_invalid"


def test_v2_capacity_gate_still_rejects_when_portfolio_is_full(
    monkeypatch,
):
    """
    The capacity gate must remain effective even with external approval.
    """

    state = default_state_v2(
        initial_equity=1000.0,
        symbols=[BTC, "ETH-SWAP-USDT"],
        max_concurrent_positions=1,
        max_portfolio_risk=0.01,
    )

    state["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {
        "side": "LONG",
        "close_at_signal": 100.0,
        "atr_at_signal": 10.0,
    }

    captured = {}

    decision = Decision(
        accepted=True,
        reason="external_approval",
        gate="external",
    )

    def fake_v1_process_candle(
        state_v1,
        klines,
        indicators,
        index,
        specs,
        config,
        signal=None,
        execution_callback=None,
        exit_callback=None,
    ):
        captured["callback_result"] = execution_callback(
            valid_entry_info(),
            state_v1,
        )
        return state_v1, []

    monkeypatch.setattr(
        v2,
        "_v1_process_candle",
        fake_v1_process_candle,
    )

    result_state, events = v2.process_symbol_candle(
        state_v2=state,
        symbol=BTC,
        klines=[],
        indicators={},
        i=0,
        specs=valid_specs(),
        config=valid_config(),
        execution_callback=lambda info, state_ref: decision,
    )

    assert result_state is state
    assert events == []

    rejection = captured["callback_result"]

    assert isinstance(rejection, dict)
    assert rejection["accepted"] is False
    assert rejection["gate"] == "portfolio_capacity"
    assert rejection["reason"] == "portfolio_capacity_exceeded"
