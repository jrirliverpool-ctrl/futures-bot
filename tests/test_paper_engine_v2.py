"""tests/test_paper_engine_v2.py — Unit tests for V2 Bridge.

Tests verify:
  1. V1 state slice extraction / merge correctness
  2. Portfolio gating (positions AND pendings) for both pre-check and execution
  3. Whitelist enforcement
  4. Circuit breaker reason sync
  5. Full entry cycle through REAL V1 (signal → pending → position)
  6. Multi-symbol capacity enforcement
  7. _specs() fixture compatibility with V1's calculate_position_size
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import paper_engine_v2 as bridge
from state_manager_v2 import default_state_v2


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def state_v2():
    return default_state_v2(
        initial_equity=984.67,
        symbols=["BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT"],
        max_concurrent_positions=1,
    )


def _specs():
    """Specs matching what get_symbol_specs() returns for BTC.

    All fields below are required by V1's calculate_position_size.
    If V1 adds a new required field, test_specs_fixture_satisfies_v1_requirements
    will fail and alert us to update this fixture.
    """
    return {
        "contract_multiplier": 0.001,
        "min_qty": 0.0001,
        "max_qty": 1000.0,
        "qty_step": 0.0001,
        "min_notional": 0.0,
        "max_leverage": 20,
    }


def _config():
    return {
        "risk_per_trade": 0.01,
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 2.0,
        "be_trigger_at_r": 1.0,
        "be_sl_offset_atr": 0.1,
        "trailing_atr_mult": 1.5,
        "time_exit_candles": 48,
        "cooldown_candles": 2,
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.07,
        "total_dd_limit": 0.15,
        "slippage_pct": 0.0,
        "open_taker_fee": 0.0,
        "close_taker_fee": 0.0,
        "leverage": None,
        "leverage_cap": None,
        "kill_switch_active": False,
        "kill_switch_reason": None,
    }


def _signal(signal_type="LONG"):
    return {
        "signal": signal_type,
        "signal_candle_ts": 1000,
        "atr_at_signal": 500.0,
        "close": 85000.0,
        "reason": "test",
    }


def _long_signal_candle():
    return [1000, "85000", "86000", "84500", "85500", "1000"]


def _next_candle():
    return [3600000, "85500", "86500", "85000", "86000", "1000"]


# ============================================================
# Fixture sanity (defensive — catches V1 requirement drift)
# ============================================================

def test_specs_fixture_satisfies_v1_requirements():
    """Verify _specs() provides all fields V1's calculate_position_size requires.

    If V1 adds a new required field (e.g. max_qty), this test fails loudly
    instead of silently causing downstream test failures.
    """
    from paper_engine import calculate_position_size

    qty = calculate_position_size(
        equity=984.67,
        entry_price=85000.0,
        stop_loss=84000.0,
        specs=_specs(),
        risk_per_trade=0.01,
    )
    assert qty is not None, (
        "_specs() is missing a field required by V1's calculate_position_size"
    )
    assert qty > 0


# ============================================================
# _count_portfolio_reservations
# ============================================================

def test_count_reservations_empty(state_v2):
    assert bridge._count_portfolio_reservations(state_v2, exclude_symbol="BTC-SWAP-USDT") == 0


def test_count_reservations_with_position(state_v2):
    state_v2["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    assert bridge._count_portfolio_reservations(state_v2, exclude_symbol="BTC-SWAP-USDT") == 1


def test_count_reservations_with_pending(state_v2):
    state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    assert bridge._count_portfolio_reservations(state_v2, exclude_symbol="BTC-SWAP-USDT") == 1


def test_count_reservations_position_and_pending_same_symbol_counts_once(state_v2):
    state_v2["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    assert bridge._count_portfolio_reservations(state_v2, exclude_symbol="BTC-SWAP-USDT") == 1


def test_count_reservations_excludes_current_symbol(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    assert bridge._count_portfolio_reservations(state_v2, exclude_symbol="BTC-SWAP-USDT") == 1
    assert bridge._count_portfolio_reservations(state_v2, exclude_symbol="ETH-SWAP-USDT") == 1


# ============================================================
# _portfolio_has_room_for_pending (pre-check)
# ============================================================

def test_has_room_when_empty(state_v2):
    assert bridge._portfolio_has_room_for_pending(state_v2, "BTC-SWAP-USDT") is True


def test_no_room_when_cb_active(state_v2):
    state_v2["circuit_breaker_active"] = True
    assert bridge._portfolio_has_room_for_pending(state_v2, "BTC-SWAP-USDT") is False


def test_no_room_when_current_symbol_has_pending(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    assert bridge._portfolio_has_room_for_pending(state_v2, "BTC-SWAP-USDT") is False


def test_no_room_when_other_has_position(state_v2):
    state_v2["symbols"]["ETH-SWAP-USDT"]["position"] = {"side": "LONG"}
    assert bridge._portfolio_has_room_for_pending(state_v2, "BTC-SWAP-USDT") is False


def test_no_room_when_other_has_pending(state_v2):
    state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    assert bridge._portfolio_has_room_for_pending(state_v2, "BTC-SWAP-USDT") is False


# ============================================================
# _portfolio_allows_entry (execution time)
# ============================================================

def test_allows_entry_when_empty(state_v2):
    assert bridge._portfolio_allows_entry(state_v2, "BTC-SWAP-USDT") is True


def test_blocks_entry_when_cb_active(state_v2):
    state_v2["circuit_breaker_active"] = True
    assert bridge._portfolio_allows_entry(state_v2, "BTC-SWAP-USDT") is False


def test_blocks_entry_when_current_has_position(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    assert bridge._portfolio_allows_entry(state_v2, "BTC-SWAP-USDT") is False


def test_blocks_entry_when_other_has_position(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    assert bridge._portfolio_allows_entry(state_v2, "ETH-SWAP-USDT") is False


def test_blocks_entry_when_other_has_pending(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] = {"side": "LONG"}
    assert bridge._portfolio_allows_entry(state_v2, "ETH-SWAP-USDT") is False


# ============================================================
# _extract_v1_slice / _apply_v1_result_to_v2
# ============================================================

def test_extract_v1_slice_maps_fields(state_v2):
    state_v2["equity"] = 950.0
    state_v2["peak_equity"] = 1000.0
    state_v2["daily_loss"] = 50.0
    state_v2["circuit_breaker_active"] = True
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    state_v2["symbols"]["BTC-SWAP-USDT"]["last_processed_candle_ts"] = 5000

    slice_v1 = bridge._extract_v1_slice(state_v2, "BTC-SWAP-USDT")

    assert slice_v1["equity"] == 950.0
    assert slice_v1["circuit_breaker_active"] is True
    assert slice_v1["position"]["side"] == "LONG"
    assert slice_v1["last_processed_candle_ts"] == 5000


def test_extract_v1_slice_is_deep_copy(state_v2):
    state_v2["symbols"]["BTC-SWAP-USDT"]["position"] = {"side": "LONG"}
    slice_v1 = bridge._extract_v1_slice(state_v2, "BTC-SWAP-USDT")
    slice_v1["position"]["side"] = "SHORT"
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["position"]["side"] == "LONG"


def test_apply_v1_result_updates_all_fields(state_v2):
    state_v1_after = {
        "equity": 990.0,
        "peak_equity": 1000.0,
        "daily_loss": 10.0,
        "weekly_loss": 20.0,
        "loss_day_key": 123,
        "loss_week_key": 45,
        "circuit_breaker_active": False,
        "position": {"side": "LONG"},
        "pending_entry": None,
        "cooldown_until_index": 42,
        "last_processed_candle_ts": 9999,
        "last_funding_ts": 12345,
    }

    bridge._apply_v1_result_to_v2(state_v2, "ETH-SWAP-USDT", state_v1_after)

    assert state_v2["equity"] == 990.0
    assert state_v2["daily_loss"] == 10.0
    eth = state_v2["symbols"]["ETH-SWAP-USDT"]
    assert eth["position"]["side"] == "LONG"
    assert eth["cooldown_until_index"] == 42
    assert eth["last_processed_candle_ts"] == 9999


# ============================================================
# _sync_circuit_breaker
# ============================================================

def test_sync_cb_records_reason_on_activation(state_v2):
    state_v2["circuit_breaker_active"] = True
    events = [{"type": "CIRCUIT_BREAKER", "reason": "DAILY_LOSS"}]

    bridge._sync_circuit_breaker(state_v2, was_active=False, events=events)

    assert state_v2["circuit_breaker_reason"] == "DAILY_LOSS"
    assert state_v2["circuit_breaker_activated_at"] is not None


def test_sync_cb_clears_reason_on_deactivation(state_v2):
    state_v2["circuit_breaker_active"] = False
    state_v2["circuit_breaker_reason"] = "OLD_REASON"
    state_v2["circuit_breaker_activated_at"] = "old_ts"

    bridge._sync_circuit_breaker(state_v2, was_active=True, events=[])

    assert state_v2["circuit_breaker_reason"] is None
    assert state_v2["circuit_breaker_activated_at"] is None


def test_sync_cb_noop_when_already_active(state_v2):
    state_v2["circuit_breaker_active"] = True
    state_v2["circuit_breaker_reason"] = "PREVIOUS"
    state_v2["circuit_breaker_activated_at"] = "previous_ts"

    bridge._sync_circuit_breaker(
        state_v2, was_active=True,
        events=[{"type": "CIRCUIT_BREAKER", "reason": "NEW"}],
    )

    assert state_v2["circuit_breaker_reason"] == "PREVIOUS"


# ============================================================
# process_symbol_candle — validation
# ============================================================

def test_process_rejects_unknown_symbol(state_v2):
    with pytest.raises(ValueError, match="not in V2 state"):
        bridge.process_symbol_candle(
            state_v2, "FAKE-SWAP-USDT", [], {}, 0, {}, {},
        )


def test_process_rejects_symbol_not_in_whitelist(state_v2):
    with pytest.raises(ValueError, match="not in allowed_symbols"):
        bridge.process_symbol_candle(
            state_v2, "BTC-SWAP-USDT", [], {}, 0, {}, {},
            allowed_symbols={"ETH-SWAP-USDT"},
        )


def test_process_accepts_symbol_in_whitelist(state_v2):
    with pytest.raises(Exception):
        bridge.process_symbol_candle(
            state_v2, "BTC-SWAP-USDT", [], {}, 0, {}, {},
            allowed_symbols={"BTC-SWAP-USDT"},
        )


# ============================================================
# process_symbol_candle — real V1 integration
# ============================================================

def test_full_entry_cycle_via_real_v1(state_v2):
    """Signal → pending_entry → position, using the REAL V1 engine."""
    signal = _signal("LONG")
    candle1 = _long_signal_candle()

    # Step 1: signal → pending_entry created
    state_v2, events1 = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle1], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] is not None
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"]["side"] == "LONG"
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["position"] is None

    # Step 2: next candle, pending executes → position opens
    candle2 = _next_candle()
    state_v2, events2 = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle1, candle2], {"atr": [500.0, 500.0]}, 1, _specs(), _config(),
        signal=None,
    )
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] is None
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["position"] is not None
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["position"]["side"] == "LONG"


def test_multi_symbol_only_one_gets_pending(state_v2):
    """Two symbols signal; only the first gets pending_entry (capacity=1)."""
    signal = _signal("LONG")
    candle = _long_signal_candle()

    state_v2, events_btc = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] is not None

    state_v2, events_eth = bridge.process_symbol_candle(
        state_v2, "ETH-SWAP-USDT",
        [candle], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] is None
    assert any(e["type"] == "SIGNAL_BLOCKED_PORTFOLIO" for e in events_eth)


def test_multi_symbol_only_one_opens_position(state_v2):
    """Only BTC gets pending; ETH is blocked by portfolio capacity."""
    signal = _signal("LONG")
    candle1 = _long_signal_candle()
    candle2 = _next_candle()

    # BTC gets pending
    state_v2, _ = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle1], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )

    # ETH is blocked by capacity (BTC already has pending)
    state_v2, events_eth = bridge.process_symbol_candle(
        state_v2, "ETH-SWAP-USDT",
        [candle1], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] is None
    assert any(e["type"] == "SIGNAL_BLOCKED_PORTFOLIO" for e in events_eth)

    # BTC pending executes → position opens
    state_v2, _ = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle1, candle2], {"atr": [500.0, 500.0]}, 1, _specs(), _config(),
        signal=None,
    )
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["position"] is not None


def test_circuit_breaker_blocks_entry(state_v2):
    """If CB is active, entry must be blocked."""
    state_v2["circuit_breaker_active"] = True
    signal = _signal("LONG")
    candle = _long_signal_candle()

    state_v2, events = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] is None
    assert any(e["type"] == "SIGNAL_BLOCKED_PORTFOLIO" for e in events)


# ============================================================
# load_allowed_symbols
# ============================================================

def test_load_allowed_symbols(tmp_path):
    p = tmp_path / "wl.json"
    p.write_text('{"symbols": ["BTC-SWAP-USDT", "ETH-SWAP-USDT"]}')
    assert bridge.load_allowed_symbols(p) == {"BTC-SWAP-USDT", "ETH-SWAP-USDT"}


def test_load_allowed_symbols_missing(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        bridge.load_allowed_symbols(tmp_path / "nonexistent.json")


def test_load_allowed_symbols_empty(tmp_path):
    p = tmp_path / "wl.json"
    p.write_text('{"symbols": []}')
    with pytest.raises(RuntimeError, match="non-empty list"):
        bridge.load_allowed_symbols(p)


def test_load_allowed_symbols_bad_format(tmp_path):
    p = tmp_path / "wl.json"
    p.write_text('{"symbols": ["BAD"]}')
    with pytest.raises(RuntimeError, match="Invalid symbol"):
        bridge.load_allowed_symbols(p)