
"""Independent regression tests for ApexQuant V2 portfolio risk."""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import paper_engine_v2 as bridge
from state_manager_v2 import default_state_v2


BTC = "BTC-SWAP-USDT"
ETH = "ETH-SWAP-USDT"


@pytest.fixture
def state():
    return default_state_v2(
        initial_equity=1000.0,
        symbols=[BTC, ETH],
        max_concurrent_positions=2,
        max_portfolio_risk=0.01,
    )


@pytest.fixture
def config():
    return {
        "risk_per_trade": 0.001,
        "sl_atr_mult": 1.5,
        "open_taker_fee": 0.0,
        "close_taker_fee": 0.0,
        "slippage_pct": 0.0,
    }


def position(
    side="LONG",
    entry=100.0,
    initial_sl=90.0,
    current_sl=90.0,
    size=1.0,
    remaining=1.0,
    risk_usd=10.0,
):
    return {
        "side": side,
        "entry_price": entry,
        "initial_sl": initial_sl,
        "current_sl": current_sl,
        "size": size,
        "remaining_size": remaining,
        "risk_usd": risk_usd,
    }


def pending(
    side="LONG",
    close=100.0,
    atr=10.0,
):
    return {
        "side": side,
        "close_at_signal": close,
        "atr_at_signal": atr,
    }


def specs():
    return {
        "contract_multiplier": 1.0,
        "min_qty": 0.001,
        "max_qty": 100000.0,
        "qty_step": 0.001,
        "min_notional": 0.0,
        "max_leverage": 20,
    }


def entry_info(
    side="LONG",
    qty=1.0,
    price=100.0,
    sl=90.0,
):
    return {
        "intent": "ENTRY",
        "side": side,
        "qty": qty,
        "price": price,
        "sl": sl,
        "tp1": 120.0,
        "leverage": 1,
        "timestamp": 1000,
    }


# ============================================================
# Position risk
# ============================================================

def test_open_long_position_risk(state, config):
    state["symbols"][BTC]["position"] = position()

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(10.0)


def test_open_short_position_risk(state, config):
    state["symbols"][BTC]["position"] = position(
        side="SHORT",
        entry=100.0,
        initial_sl=110.0,
        current_sl=110.0,
        risk_usd=10.0,
    )

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(10.0)


def test_position_risk_reduces_after_tp1(state, config):
    state["symbols"][BTC]["position"] = position(
        size=1.0,
        remaining=0.5,
    )

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(5.0)


def test_position_risk_updates_after_stop_move(state, config):
    state["symbols"][BTC]["position"] = position(
        current_sl=95.0,
        remaining=0.5,
    )

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(2.5)


def test_profitable_stop_has_no_gross_downside_risk(state, config):
    state["symbols"][BTC]["position"] = position(
        current_sl=105.0,
        remaining=0.5,
    )

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(0.0)


# ============================================================
# Invalid position state
# ============================================================

@pytest.mark.parametrize(
    "changes",
    [
        {"entry_price": float("nan")},
        {"current_sl": float("inf")},
        {"size": 0.0},
        {"remaining_size": 2.0},
        {"risk_usd": -1.0},
        {"initial_sl": 100.0},
        {"side": "UNKNOWN"},
    ],
)
def test_invalid_position_fails_closed(
    state,
    config,
    changes,
):
    pos = position()
    pos.update(changes)

    state["symbols"][BTC]["position"] = pos

    assert bridge._portfolio_risk_used_usd(
        state,
        config,
    ) is None


# ============================================================
# Pending reservations
# ============================================================

def test_pending_entry_reserves_risk(state, config):
    state["symbols"][BTC]["pending_entry"] = pending()

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(1.0)


def test_pending_entry_includes_execution_costs(config):
    cfg = dict(config)
    cfg["open_taker_fee"] = 0.0005
    cfg["close_taker_fee"] = 0.0005
    cfg["slippage_pct"] = 0.0005

    risk = bridge._pending_risk_reservation_usd(
        pending(),
        1000.0,
        cfg,
    )

    assert risk > 1.0


def test_invalid_pending_fails_closed(state, config):
    state["symbols"][BTC]["pending_entry"] = {
        "side": "LONG",
        "close_at_signal": 100.0,
    }

    assert bridge._portfolio_risk_used_usd(
        state,
        config,
    ) is None


def test_pending_and_position_same_symbol_fails_closed(
    state,
    config,
):
    state["symbols"][BTC]["position"] = position()
    state["symbols"][BTC]["pending_entry"] = pending()

    assert bridge._portfolio_risk_used_usd(
        state,
        config,
    ) is None


# ============================================================
# Portfolio risk calculation
# ============================================================

def test_multiple_positions_are_summed(state, config):
    state["symbols"][BTC]["position"] = position()
    state["symbols"][ETH]["position"] = position(
        entry=200.0,
        initial_sl=190.0,
        current_sl=190.0,
        risk_usd=20.0,
    )

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(30.0)


def test_positions_and_pending_are_summed(state, config):
    state["symbols"][BTC]["position"] = position()
    state["symbols"][ETH]["pending_entry"] = pending()

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
    )

    assert risk == pytest.approx(11.0)


def test_excluded_symbol_is_not_counted(state, config):
    state["symbols"][BTC]["position"] = position()
    state["symbols"][ETH]["pending_entry"] = pending()

    risk = bridge._portfolio_risk_used_usd(
        state,
        config,
        exclude_symbol=BTC,
    )

    assert risk == pytest.approx(1.0)


# ============================================================
# Portfolio limits
# ============================================================

def test_portfolio_risk_limit_is_equity_times_fraction(state):
    assert bridge._portfolio_risk_limit(state) == 10.0


def test_invalid_equity_fails_closed(state):
    state["equity"] = 0.0

    assert bridge._portfolio_risk_limit(state) is None


def test_invalid_portfolio_fraction_fails_closed(state):
    state["max_portfolio_risk"] = 0.0

    assert bridge._portfolio_risk_limit(state) is None


def test_pending_signal_allowed_within_budget(
    state,
    config,
):
    signal = {
        "signal": "LONG",
        "close": 100.0,
        "atr_at_signal": 10.0,
    }

    allowed, reason = bridge._portfolio_risk_allows_pending(
        state,
        BTC,
        signal,
        config,
    )

    assert allowed is True
    assert reason is None


def test_pending_signal_rejected_above_budget(
    state,
    config,
):
    cfg = dict(config)
    cfg["risk_per_trade"] = 0.02

    signal = {
        "signal": "LONG",
        "close": 100.0,
        "atr_at_signal": 10.0,
    }

    allowed, reason = bridge._portfolio_risk_allows_pending(
        state,
        BTC,
        signal,
        cfg,
    )

    assert allowed is False
    assert reason == "portfolio_risk_limit_exceeded"


# ============================================================
# Execution-time risk
# ============================================================

def test_actual_entry_risk_calculation(config):
    risk = bridge._new_entry_risk_usd(
        entry_info(qty=1.0),
        specs(),
        config,
    )

    assert risk == pytest.approx(10.0)


def test_actual_entry_includes_fees_and_slippage(config):
    cfg = dict(config)
    cfg["open_taker_fee"] = 0.0005
    cfg["close_taker_fee"] = 0.0005
    cfg["slippage_pct"] = 0.0005

    risk = bridge._new_entry_risk_usd(
        entry_info(),
        specs(),
        cfg,
    )

    assert risk > 10.0


def test_invalid_entry_fails_closed(config):
    info = entry_info()
    info["qty"] = float("nan")

    assert bridge._new_entry_risk_usd(
        info,
        specs(),
        config,
    ) is None


def test_entry_exactly_at_portfolio_limit_is_allowed(
    state,
    config,
):
    allowed, reason = bridge._portfolio_risk_allows_entry(
        state,
        BTC,
        entry_info(qty=1.0),
        specs(),
        config,
    )

    assert allowed is True
    assert reason is None


def test_entry_above_portfolio_limit_is_rejected(
    state,
    config,
):
    allowed, reason = bridge._portfolio_risk_allows_entry(
        state,
        BTC,
        entry_info(qty=1.01),
        specs(),
        config,
    )

    assert allowed is False
    assert reason == "portfolio_risk_limit_exceeded"


def test_existing_risk_reduces_available_capacity(
    state,
    config,
):
    state["symbols"][ETH]["position"] = position()

    allowed, reason = bridge._portfolio_risk_allows_entry(
        state,
        BTC,
        entry_info(qty=0.01),
        specs(),
        config,
    )

    assert allowed is False
    assert reason == "portfolio_risk_limit_exceeded"


# ============================================================
# Execution callback integration
# ============================================================

def test_execution_callback_blocks_entry_over_portfolio_limit(
    state,
    config,
    monkeypatch,
):
    state["symbols"][BTC]["pending_entry"] = pending()

    captured = {}

    def fake_v1(
        state_v1,
        klines,
        indicators,
        index,
        symbol_specs,
        engine_config,
        signal=None,
        execution_callback=None,
        exit_callback=None,
    ):
        captured["decision"] = execution_callback(
            entry_info(qty=1.01),
            state_v1,
        )
        return state_v1, []

    monkeypatch.setattr(
        bridge,
        "_v1_process_candle",
        fake_v1,
    )

    result_state, events = bridge.process_symbol_candle(
        state_v2=state,
        symbol=BTC,
        klines=[],
        indicators={},
        i=0,
        specs=specs(),
        config=config,
        signal=None,
    )

    assert result_state is state
    assert captured["decision"]["accepted"] is False
    assert captured["decision"]["gate"] == "portfolio_risk"
    assert (
        captured["decision"]["reason"]
        == "portfolio_risk_limit_exceeded"
    )


def test_external_decision_object_is_preserved(
    state,
    config,
    monkeypatch,
):
    class Decision:
        accepted = False
        reason = "external_rejection"
        gate = "external_gate"

    decision = Decision()
    captured = {}

    def fake_v1(
        state_v1,
        klines,
        indicators,
        index,
        symbol_specs,
        engine_config,
        signal=None,
        execution_callback=None,
        exit_callback=None,
    ):
        captured["result"] = execution_callback(
            {"intent": "OTHER"},
            state_v1,
        )
        return state_v1, []

    monkeypatch.setattr(
        bridge,
        "_v1_process_candle",
        fake_v1,
    )

    bridge.process_symbol_candle(
        state_v2=state,
        symbol=BTC,
        klines=[],
        indicators={},
        i=0,
        specs=specs(),
        config=config,
        signal=None,
        execution_callback=lambda info, ref: decision,
    )

    assert captured["result"] is decision
    assert captured["result"].accepted is False
    assert captured["result"].reason == "external_rejection"
    assert captured["result"].gate == "external_gate"
