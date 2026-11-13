
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


class Decision:
    def __init__(self, accepted, reason=None, gate=None):
        self.accepted = accepted
        self.reason = reason
        self.gate = gate


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
    symbol = "BTC-SWAP-USDT"

    state = default_state_v2(
        initial_equity=1000.0,
        symbols=[symbol],
        max_concurrent_positions=1,
    )

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
        symbol=symbol,
        klines=[],
        indicators={},
        i=0,
        specs={},
        config={},
        execution_callback=lambda info, state_ref: decision,
    )

    assert result_state is state
    assert events == []
    assert captured["callback_result"] is decision
    assert captured["callback_result"].accepted is accepted
    assert captured["callback_result"].reason == "test_decision"
    assert captured["callback_result"].gate == "test_gate"
