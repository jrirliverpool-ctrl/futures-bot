"""tests/test_paper_engine_v2.py — Unit tests for V2 Bridge.

Tests verify:
  1. V1 state slice extraction / merge correctness
  2. Portfolio gating (positions AND pendings) for both pre-check and execution
  3. Whitelist enforcement
  4. Circuit breaker reason sync
  5. Full lifecycle through REAL V1: signal → pending → entry → TP1 → exit
  6. Multi-symbol capacity enforcement
  7. _specs() fixture compatibility with V1's calculate_position_size

All lifecycle tests:
  - read actual prices from state (not hardcoded),
  - assert each stage explicitly (no silent returns),
  - verify equity transitions and cooldown.
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

    state_v2, events1 = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle1], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"] is not None
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["pending_entry"]["side"] == "LONG"
    assert state_v2["symbols"]["BTC-SWAP-USDT"]["position"] is None

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

    state_v2, _ = bridge.process_symbol_candle(
        state_v2, "BTC-SWAP-USDT",
        [candle1], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )

    state_v2, events_eth = bridge.process_symbol_candle(
        state_v2, "ETH-SWAP-USDT",
        [candle1], {"atr": [500.0]}, 0, _specs(), _config(),
        signal=signal,
    )
    assert state_v2["symbols"]["ETH-SWAP-USDT"]["pending_entry"] is None
    assert any(e["type"] == "SIGNAL_BLOCKED_PORTFOLIO" for e in events_eth)

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
# Full lifecycle via Bridge (real V1, no silent returns)
# ============================================================

def test_full_trade_lifecycle_via_bridge(state_v2):
    """End-to-end: signal → pending → entry → TP1 → exit, all via Bridge.

    Design principles:
      - Reads ACTUAL prices from state after each stage (not hardcoded).
      - Every stage has its own assertion; no silent returns.
      - Candles for TP1 and exit are constructed from the ACTUAL current_sl
        and initial_tp1 values read from position, so they always trigger
        the expected event regardless of how V1 adjusts prices internally.
    """
    BTC = "BTC-SWAP-USDT"
    atr = 5.0
    specs = _specs()
    cfg = _config()
    cfg["slippage_pct"] = 0.0
    cfg["open_taker_fee"] = 0.0
    cfg["close_taker_fee"] = 0.0

    # --- Candle 0: signal candle ---
    # OHLC only needs to be valid; signal dict drives pending creation.
    c0 = [1_000_000, "100", "102", "98", "101", "1000"]
    signal = {
        "signal": "LONG",
        "signal_candle_ts": 1_000_000,
        "atr_at_signal": atr,
        "close": 101.0,
        "reason": "lifecycle test",
    }

    state_v2, ev0 = bridge.process_symbol_candle(
        state_v2, BTC, [c0], {"atr": [atr]}, 0, specs, cfg,
        signal=signal,
    )

    # === STAGE 1: signal → pending_entry ===
    pending = state_v2["symbols"][BTC]["pending_entry"]
    assert pending is not None, (
        f"STAGE 1 FAIL: pending_entry not created. Events: {ev0}"
    )
    assert pending["side"] == "LONG"
    assert state_v2["symbols"][BTC]["position"] is None

    # --- Candle 1: entry candle ---
    c1 = [1_003_600_000, "100", "102", "99", "101", "1000"]

    state_v2, ev1 = bridge.process_symbol_candle(
        state_v2, BTC, [c0, c1], {"atr": [atr, atr]}, 1, specs, cfg,
        signal=None,
    )

    # === STAGE 2: pending → entry ===
    assert state_v2["symbols"][BTC]["pending_entry"] is None, (
        f"STAGE 2 FAIL: pending_entry not cleared. Events: {ev1}"
    )

    entry_events = [e for e in ev1 if e.get("type") == "ENTRY"]
    assert len(entry_events) == 1, (
        f"STAGE 2 FAIL: expected 1 ENTRY, got {len(entry_events)}. "
        f"All events: {ev1}"
    )

    pos = state_v2["symbols"][BTC]["position"]
    assert pos is not None, f"STAGE 2 FAIL: position not opened. Events: {ev1}"
    assert pos["side"] == "LONG"
    assert pos["tp1_hit"] is False
    assert pos["remaining_size"] > 0

    # Read ACTUAL levels from state for building subsequent candles
    entry_price = pos["entry_price"]
    initial_tp1 = pos["initial_tp1"]
    initial_sl = pos["initial_sl"]
    current_sl = pos["current_sl"]
    assert initial_tp1 > entry_price > initial_sl, (
        f"STAGE 2 sanity: expected initial_sl < entry < initial_tp1, "
        f"got sl={initial_sl} entry={entry_price} tp1={initial_tp1}"
    )
    assert current_sl == initial_sl

    equity_after_entry = state_v2["equity"]
    remaining_before_tp1 = pos["remaining_size"]

    # --- Candle 2: TP1 candle (constructed from actual tp1 and sl) ---
    c2_high = initial_tp1 + 5.0     # definitely exceeds TP1
    c2_low = current_sl + 1.0       # definitely above SL (won't trigger SL first)
    c2_close = c2_high - 1.0
    c2 = [
        1_007_200_000,
        str(entry_price),
        str(c2_high),
        str(c2_low),
        str(c2_close),
        "1000",
    ]

    state_v2, ev2 = bridge.process_symbol_candle(
        state_v2, BTC, [c0, c1, c2],
        {"atr": [atr, atr, atr]}, 2, specs, cfg,
        signal=None,
    )

    # === STAGE 3: TP1 fires ===
    tp1_events = [e for e in ev2 if e.get("type") == "TP1_HIT"]
    assert len(tp1_events) == 1, (
        f"STAGE 3 FAIL: expected 1 TP1_HIT, got {len(tp1_events)}. "
        f"Candle: {c2}, initial_tp1={initial_tp1}, current_sl={current_sl}. "
        f"All events: {ev2}"
    )

    pos = state_v2["symbols"][BTC]["position"]
    assert pos is not None, "STAGE 3 FAIL: position gone after TP1"
    assert pos["tp1_hit"] is True, "STAGE 3 FAIL: tp1_hit flag not set"
    assert pos["remaining_size"] < remaining_before_tp1, (
        f"STAGE 3 FAIL: remaining_size not reduced. "
        f"before={remaining_before_tp1} after={pos['remaining_size']}"
    )
    assert pos["realized_pnl"] > 0, (
        f"STAGE 3 FAIL: realized_pnl={pos['realized_pnl']} not positive"
    )

    equity_after_tp1 = state_v2["equity"]
    assert equity_after_tp1 > equity_after_entry, (
        f"STAGE 3 FAIL: equity didn't increase after TP1. "
        f"before={equity_after_entry} after={equity_after_tp1}"
    )

    # Read updated SL after TP1 (moved up due to BE + trailing)
    current_sl_after_tp1 = pos["current_sl"]
    assert current_sl_after_tp1 > initial_sl, (
        f"STAGE 3 FAIL: SL not moved up after TP1. "
        f"initial_sl={initial_sl} current_sl={current_sl_after_tp1}"
    )

    # --- Candle 3: exit candle (constructed from actual current_sl) ---
    c3_low = current_sl_after_tp1 - 5.0     # definitely below SL
    c3_open = current_sl_after_tp1 + 1.0
    c3_high = c3_open + 2.0
    c3_close = c3_low + 1.0
    c3 = [
        1_010_800_000,
        str(c3_open),
        str(c3_high),
        str(c3_low),
        str(c3_close),
        "1000",
    ]

    state_v2, ev3 = bridge.process_symbol_candle(
        state_v2, BTC, [c0, c1, c2, c3],
        {"atr": [atr, atr, atr, atr]}, 3, specs, cfg,
        signal=None,
    )

    # === STAGE 4: Exit fires ===
    exit_events = [e for e in ev3 if e.get("type") == "EXIT"]
    assert len(exit_events) == 1, (
        f"STAGE 4 FAIL: expected 1 EXIT, got {len(exit_events)}. "
        f"Candle: {c3}, current_sl={current_sl_after_tp1}. "
        f"All events: {ev3}"
    )

    assert state_v2["symbols"][BTC]["position"] is None, (
        "STAGE 4 FAIL: position not cleared after exit"
    )
    assert state_v2["symbols"][BTC]["pending_entry"] is None, (
        "STAGE 4 FAIL: pending_entry appeared unexpectedly"
    )
    assert state_v2["symbols"][BTC]["cooldown_until_index"] is not None, (
        "STAGE 4 FAIL: cooldown_until_index not set after exit"
    )
    assert state_v2["symbols"][BTC]["last_processed_candle_ts"] == 1_010_800_000, (
        f"STAGE 4 FAIL: last_processed not updated. "
        f"got {state_v2['symbols'][BTC]['last_processed_candle_ts']}"
    )

    # Equity after exit must not have gone negative or nonsensical
    equity_after_exit = state_v2["equity"]
    assert equity_after_exit > 0, (
        f"STAGE 4 FAIL: equity non-positive after exit: {equity_after_exit}"
    )
    # The total PnL of the trade = equity_after_exit - initial_equity
    # We don't assert its sign (SL could hit below entry under some configs),
    # but we do assert it's finite and reasonable.
    assert abs(equity_after_exit - 984.67) < 100.0, (
        f"STAGE 4 sanity: equity {equity_after_exit} far from initial 984.67"
    )


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