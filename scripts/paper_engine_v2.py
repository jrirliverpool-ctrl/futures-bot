"""paper_engine_v2.py — Multi-symbol Bridge for ApexQuant V2.

This module wraps V1's process_candle (UNCHANGED) and adds:
  1. Per-symbol state isolation (V1 works on a per-symbol slice)
  2. Portfolio-level entry gating — counts BOTH positions AND pending entries
  3. Symbol whitelist enforcement — at API boundary, not just at load
  4. Circuit breaker reason/activated_at synchronization
  5. Clean merge-back of V1 result into V2 state

CRITICAL:
  - V1 (`paper_engine.py`) is NEVER modified.
  - This file only orchestrates state extraction/merge.
  - V1.0.1 strategy logic is reused verbatim.

Design reference: docs/v2_multi_symbol_design.md (section 4).
"""

import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import process_candle as _v1_process_candle


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CRYPTO_WHITELIST_PATH = ROOT / "config" / "crypto_whitelist.json"


# ============================================================
# Whitelist loading
# ============================================================

def load_allowed_symbols(path: Path = None) -> set:
    """Load allowed symbols from crypto_whitelist.json.

    Fails closed: missing or malformed file raises RuntimeError.
    Called by run_multi_symbol.py at startup.
    """
    if path is None:
        path = CRYPTO_WHITELIST_PATH

    if not path.exists():
        raise RuntimeError(f"Crypto whitelist not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise RuntimeError(f"Crypto whitelist must be a JSON object: {path}")

    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError(f"'symbols' must be a non-empty list in {path}")

    validated = set()
    for sym in symbols:
        if not isinstance(sym, str) or not sym.endswith("-SWAP-USDT"):
            raise RuntimeError(f"Invalid symbol in crypto whitelist: {sym!r}")
        if sym in validated:
            raise RuntimeError(f"Duplicate symbol in crypto whitelist: {sym!r}")
        validated.add(sym)

    return validated


# ============================================================
# Portfolio capacity checks
# ============================================================

def _count_portfolio_reservations(state_v2: dict, exclude_symbol: str) -> int:
    """Count 'reservations' from OTHER symbols.

    A symbol reserves capacity if it has:
      - an open position, OR
      - a pending_entry (a queued order that will likely become a position)

    We exclude the current symbol, because it's being processed right now
    and we're deciding whether to add its own new reservation.
    """
    count = 0
    for sym, s in state_v2["symbols"].items():
        if sym == exclude_symbol:
            continue
        if s["position"] is not None:
            count += 1
        elif s["pending_entry"] is not None:
            count += 1
    return count


def _portfolio_has_room_for_pending(state_v2: dict, symbol: str) -> bool:
    """True if we can create a NEW pending_entry for `symbol`.

    Called BEFORE passing a signal to V1. If False, we suppress the
    signal so V1 doesn't create a pending_entry that would exceed
    portfolio capacity.

    Rules:
      - circuit_breaker blocks everything
      - current symbol must not already have pending_entry
      - OTHER reservations (positions + pendings) < max_concurrent
    """
    if state_v2.get("circuit_breaker_active", False):
        return False

    if state_v2["symbols"][symbol]["pending_entry"] is not None:
        return False

    other_reserved = _count_portfolio_reservations(state_v2, exclude_symbol=symbol)
    return other_reserved < state_v2["max_concurrent_positions"]


def _portfolio_allows_entry(state_v2: dict, current_symbol: str) -> bool:
    """True if an EXISTING pending_entry on `current_symbol` may execute.

    Called from inside V1's execution_callback, BEFORE V1 commits the
    position. At that moment state_v2 still reflects pre-entry state
    for current_symbol.

    NOTE: counts OTHER reservations (positions + pendings) too, for
    defense-in-depth. This catches race conditions where a second
    pending somehow slipped through the pre-check.
    """
    if state_v2.get("circuit_breaker_active", False):
        return False

    if state_v2["symbols"][current_symbol]["position"] is not None:
        return False

    other_reserved = _count_portfolio_reservations(state_v2, exclude_symbol=current_symbol)
    return other_reserved < state_v2["max_concurrent_positions"]


# ============================================================
# State extraction / merge
# ============================================================

def _extract_v1_slice(state_v2: dict, symbol: str) -> dict:
    """Build a V1-compatible state dict from the V2 state for one symbol.

    V1's process_candle expects a flat dict mixing portfolio-level and
    symbol-level fields. We extract the symbol's slice combined with
    shared portfolio fields.
    """
    sym = state_v2["symbols"][symbol]
    return {
        # Portfolio-level (shared)
        "initial_equity": state_v2["initial_equity"],
        "equity": state_v2["equity"],
        "peak_equity": state_v2["peak_equity"],
        "daily_loss": state_v2["daily_loss"],
        "weekly_loss": state_v2["weekly_loss"],
        "loss_day_key": state_v2["loss_day_key"],
        "loss_week_key": state_v2["loss_week_key"],
        "circuit_breaker_active": state_v2["circuit_breaker_active"],

        # Per-symbol
        "position": copy.deepcopy(sym["position"]),
        "pending_entry": copy.deepcopy(sym["pending_entry"]),
        "cooldown_until_index": sym["cooldown_until_index"],
        "last_processed_candle_ts": sym["last_processed_candle_ts"],
        "last_funding_ts": sym["last_funding_ts"],
    }


def _apply_v1_result_to_v2(state_v2: dict, symbol: str, state_v1_after: dict) -> None:
    """Copy V1's post-processing state back into V2.

    Every field must be written explicitly. A missed field means drift.
    """
    state_v2["equity"] = state_v1_after["equity"]
    state_v2["peak_equity"] = state_v1_after["peak_equity"]
    state_v2["daily_loss"] = state_v1_after["daily_loss"]
    state_v2["weekly_loss"] = state_v1_after["weekly_loss"]
    state_v2["loss_day_key"] = state_v1_after["loss_day_key"]
    state_v2["loss_week_key"] = state_v1_after["loss_week_key"]
    state_v2["circuit_breaker_active"] = state_v1_after["circuit_breaker_active"]

    sym = state_v2["symbols"][symbol]
    sym["position"] = state_v1_after["position"]
    sym["pending_entry"] = state_v1_after["pending_entry"]
    sym["cooldown_until_index"] = state_v1_after["cooldown_until_index"]
    sym["last_processed_candle_ts"] = state_v1_after["last_processed_candle_ts"]
    sym["last_funding_ts"] = state_v1_after["last_funding_ts"]


def _sync_circuit_breaker(state_v2: dict, was_active: bool, events: list) -> None:
    """Sync circuit_breaker_reason and activated_at when CB flips."""
    now_active = state_v2["circuit_breaker_active"]

    if (not was_active) and now_active:
        cb_event = next(
            (e for e in events if e.get("type") == "CIRCUIT_BREAKER"),
            None,
        )
        reason = cb_event.get("reason") if cb_event else "unknown"
        state_v2["circuit_breaker_reason"] = reason
        state_v2["circuit_breaker_activated_at"] = datetime.now(timezone.utc).isoformat()
    elif was_active and not now_active:
        # Defensive: don't leave stale reason if it somehow deactivates
        state_v2["circuit_breaker_reason"] = None
        state_v2["circuit_breaker_activated_at"] = None


# ============================================================
# Main entry point
# ============================================================

def process_symbol_candle(
    state_v2: dict,
    symbol: str,
    klines: list,
    indicators: dict,
    i: int,
    specs: dict,
    config: dict,
    signal=None,
    execution_callback=None,
    exit_callback=None,
    allowed_symbols=None,
) -> tuple:
    """Process one candle for one symbol via V1, then merge back to V2.

    Args:
        state_v2: V2 state dict (mutated in place).
        symbol: The symbol to process. Must exist in state_v2["symbols"]
            AND, if allowed_symbols is provided, must be in allowed_symbols.
        klines, indicators, i, specs, config: passed to V1 verbatim.
        signal: optional signal dict from strategy.
        execution_callback: optional user callback for entry decisions.
            Called AFTER portfolio gating passes.
        exit_callback: optional user callback for exit decisions.
        allowed_symbols: optional whitelist set. If provided, `symbol`
            must be in it, otherwise ValueError is raised. Defense in
            depth — state_v2["symbols"] should already be the whitelist.

    Returns:
        (state_v2, events). state_v2 is the same object mutated in place.

    Raises:
        ValueError if symbol not in state, or not in allowed_symbols.
    """
    # === 1. Whitelist enforcement (defense in depth) ===
    if symbol not in state_v2["symbols"]:
        raise ValueError(
            f"Symbol {symbol!r} not in V2 state; "
            f"ensure it's in crypto_whitelist.json at state initialization"
        )
    if allowed_symbols is not None and symbol not in allowed_symbols:
        raise ValueError(
            f"Symbol {symbol!r} not in allowed_symbols whitelist"
        )

    # === 2. Pre-check: suppress signal if pending capacity full ===
    events_from_bridge = []
    original_signal = signal
    if (
        signal is not None
        and signal.get("signal") in ("LONG", "SHORT")
        and not _portfolio_has_room_for_pending(state_v2, symbol)
    ):
        events_from_bridge.append({
            "type": "SIGNAL_BLOCKED_PORTFOLIO",
            "symbol": symbol,
            "original_signal": signal.get("signal"),
            "reason": "portfolio_capacity_full",
            "timestamp": int(klines[i][0]),
        })
        logger.info(
            "Suppressed %s signal on %s: portfolio capacity full "
            "(max_concurrent=%d, other_reserved=%d)",
            signal.get("signal"), symbol,
            state_v2["max_concurrent_positions"],
            _count_portfolio_reservations(state_v2, exclude_symbol=symbol),
        )
        signal = None

    # === 3. Build V1 slice ===
    state_v1 = _extract_v1_slice(state_v2, symbol)

    # === 4. Combined callback: portfolio gate + user callback ===
    def combined_execution_callback(info):
        if info.get("intent") == "ENTRY":
            if not _portfolio_allows_entry(state_v2, symbol):
                logger.info(
                    "Portfolio gate blocked ENTRY on %s "
                    "(max_concurrent=%d, other_reserved=%d)",
                    symbol, state_v2["max_concurrent_positions"],
                    _count_portfolio_reservations(state_v2, exclude_symbol=symbol),
                )
                return False
        if execution_callback is not None:
            return execution_callback(info)
        return True

    # === 5. Call V1 (unchanged) ===
    was_cb_active = state_v2["circuit_breaker_active"]
    state_v1_after, v1_events = _v1_process_candle(
        state_v1,
        klines,
        indicators,
        i,
        specs,
        config,
        signal=signal,
        execution_callback=combined_execution_callback,
        exit_callback=exit_callback,
    )

    # === 6. Merge V1 result back to V2 ===
    _apply_v1_result_to_v2(state_v2, symbol, state_v1_after)

    # === 7. Sync circuit breaker reason/activated_at ===
    _sync_circuit_breaker(state_v2, was_cb_active, v1_events)

    # === 8. Combine events ===
    all_events = events_from_bridge + v1_events
    return state_v2, all_events