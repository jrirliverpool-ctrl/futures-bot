"""
state_manager_v2.py — Atomic State validation for ApexQuant V2 (multi-symbol).

Completely independent from V1's state_manager.py.
Never reads or writes paper_state_dir/.

CRITICAL: delete_state_v2 is NOT in the normal API. Deletion is an administrative
operation that must go through the CLI (`python -m scripts.state_manager_v2_admin`)
or manual filesystem operations. This prevents accidental data loss during
normal execution.

This module represents SIMULATION ONLY. Real exchange trading requires a
separate execution layer that is not implemented here.
"""

import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone

from compute_hash import compute_strategy_hash


V2_SCHEMA_VERSION = "2.0.0"
V2_SCHEMA_NAME = "multi_symbol_v1"

DEFAULT_INITIAL_EQUITY = 984.67

DEFAULT_SYMBOLS = [
    "BTC-SWAP-USDT",
    "ETH-SWAP-USDT",
    "SOL-SWAP-USDT",
]

# Per-symbol state field names (exact match required)
SYMBOL_STATE_FIELDS = frozenset({
    "last_processed_candle_ts",
    "cooldown_until_index",
    "pending_entry",
    "position",
    "last_funding_ts",
})

# All required top-level fields (exact match required)
REQUIRED_TOP_LEVEL_FIELDS = frozenset({
    "version", "schema",
    "strategy_hash", "strategy_version",
    "initial_equity", "equity", "peak_equity",
    "initial_equity_source",
    "created_at_utc", "updated_at_utc",
    "loss_day_key", "loss_week_key",
    "daily_loss", "weekly_loss", "period_start_utc",
    "circuit_breaker_active", "circuit_breaker_reason", "circuit_breaker_activated_at",
    "max_concurrent_positions", "max_portfolio_risk",
    "symbols",
})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_symbol_state() -> dict:
    return {
        "last_processed_candle_ts": None,
        "cooldown_until_index": None,
        "pending_entry": None,
        "position": None,
        "last_funding_ts": None,
    }


# ============================================================
# Default state
# ============================================================

def default_state_v2(
    initial_equity: float = DEFAULT_INITIAL_EQUITY,
    symbols: list = None,
    max_concurrent_positions: int = 1,
    max_portfolio_risk: float = 0.01,
) -> dict:
    """Create a fresh V2 state (portfolio-level, multi-symbol)."""

    if isinstance(initial_equity, bool) or not isinstance(initial_equity, (int, float)):
        raise ValueError("initial_equity must be a number")
    if not math.isfinite(initial_equity):
        raise ValueError("initial_equity must be finite")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    if symbols is None:
        symbols = list(DEFAULT_SYMBOLS)
    if not isinstance(symbols, list) or len(symbols) == 0:
        raise ValueError("symbols must be a non-empty list")

    if isinstance(max_concurrent_positions, bool) or not isinstance(max_concurrent_positions, int):
        raise ValueError("max_concurrent_positions must be an int")
    if max_concurrent_positions < 1:
        raise ValueError("max_concurrent_positions must be >= 1")

    if isinstance(max_portfolio_risk, bool) or not isinstance(max_portfolio_risk, (int, float)):
        raise ValueError("max_portfolio_risk must be a number")
    if not (0 < max_portfolio_risk <= 1):
        raise ValueError("max_portfolio_risk must be in (0, 1]")

    strategy_hash = compute_strategy_hash()
    now_iso = _utc_now_iso()

    symbol_states = {sym: _new_symbol_state() for sym in symbols}

    return {
        "version": V2_SCHEMA_VERSION,
        "schema": V2_SCHEMA_NAME,

        "strategy_hash": strategy_hash,
        "strategy_version": "1.0.1",

        "initial_equity": float(initial_equity),
        "equity": float(initial_equity),
        "peak_equity": float(initial_equity),

        "initial_equity_source": (
            "V2 bootstrapped from simulation; independent of V1 and exchange balance"
        ),
        "created_at_utc": now_iso,
        "updated_at_utc": now_iso,

        "loss_day_key": None,
        "loss_week_key": None,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "period_start_utc": now_iso,

        "circuit_breaker_active": False,
        "circuit_breaker_reason": None,
        "circuit_breaker_activated_at": None,

        "max_concurrent_positions": max_concurrent_positions,
        "max_portfolio_risk": float(max_portfolio_risk),

        "symbols": symbol_states,
    }


# ============================================================
# Validation
# ============================================================

def _check_finite_recursive(obj, path):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite_recursive(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_finite_recursive(v, f"{path}[{i}]")
    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"Invalid numeric value at {path}: {obj}")


def _is_valid_symbol_format(sym) -> bool:
    """Basic format check. Real validation requires the whitelist."""
    return isinstance(sym, str) and len(sym) > 0 and sym.endswith("-USDT")


def validate_state_v2(
    state: dict,
    allowed_symbols: list = None,
    check_hash: bool = False,
) -> None:
    """
    Strict validation of V2 state. Raises ValueError on any violation.

    Args:
        state: The state dict to validate.
        allowed_symbols: Optional whitelist. If provided, every symbol key must be in it.
                        If None, only format check is performed (must end with -USDT).
        check_hash: If True, compare strategy_hash against the current computed hash.
                   Should be True on load, False on save (since save may happen when
                   strategy config hasn't changed but engine is being tested).
    """

    if not isinstance(state, dict):
        raise ValueError("state must be a dict")

    # 1. Exact top-level field match
    actual_top = set(state.keys())
    if actual_top != REQUIRED_TOP_LEVEL_FIELDS:
        missing = REQUIRED_TOP_LEVEL_FIELDS - actual_top
        extra = actual_top - REQUIRED_TOP_LEVEL_FIELDS
        raise ValueError(
            f"Top-level fields mismatch. Missing: {sorted(missing)}, Extra: {sorted(extra)}"
        )

    # 2. Version + schema
    if state["version"] != V2_SCHEMA_VERSION:
        raise ValueError(
            f"version must be {V2_SCHEMA_VERSION!r}, got {state['version']!r}"
        )
    if state["schema"] != V2_SCHEMA_NAME:
        raise ValueError(
            f"schema must be {V2_SCHEMA_NAME!r}, got {state['schema']!r}"
        )

    # 3. Strategy hash type check (and optional comparison)
    if not isinstance(state["strategy_hash"], str) or not state["strategy_hash"]:
        raise ValueError("strategy_hash must be a non-empty string")
    if check_hash:
        try:
            current = compute_strategy_hash()
        except Exception as e:
            raise ValueError(f"Could not compute current strategy hash: {e}")
        if state["strategy_hash"] != current:
            raise ValueError(
                f"strategy_hash mismatch. Saved: {state['strategy_hash']}, "
                f"Current: {current}"
            )

    # 4. Recursive finite check
    _check_finite_recursive(state, "state")

    # 5. Equity fields
    #    - initial_equity: must be > 0 (starting capital)
    #    - equity:         must be >= 0 (can be 0 if account blown; engine must halt)
    #    - peak_equity:    must be >= initial_equity (peak is at least initial)
    for k in ("initial_equity", "equity", "peak_equity"):
        v = state[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number")
        if not math.isfinite(v):
            raise ValueError(f"{k} must be finite")

    if state["initial_equity"] <= 0:
        raise ValueError("initial_equity must be > 0 (starting capital)")
    if state["equity"] < 0:
        raise ValueError("equity must be >= 0 (0 means account blown, engine should halt)")
    if state["peak_equity"] < state["initial_equity"]:
        raise ValueError("peak_equity must be >= initial_equity")

    # 6. Daily/weekly loss (must be >= 0 and finite)
    for k in ("daily_loss", "weekly_loss"):
        v = state[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number")
        if not math.isfinite(v):
            raise ValueError(f"{k} must be finite")
        if v < 0:
            raise ValueError(f"{k} must be >= 0")

    # 7. Circuit breaker
    if not isinstance(state["circuit_breaker_active"], bool):
        raise ValueError("circuit_breaker_active must be a bool")

    # 8. Portfolio limits
    mcp = state["max_concurrent_positions"]
    if isinstance(mcp, bool) or not isinstance(mcp, int) or mcp < 1:
        raise ValueError("max_concurrent_positions must be int >= 1")

    mpr = state["max_portfolio_risk"]
    if isinstance(mpr, bool) or not isinstance(mpr, (int, float)):
        raise ValueError("max_portfolio_risk must be a number")
    if not (0 < mpr <= 1):
        raise ValueError("max_portfolio_risk must be in (0, 1]")

    # 9. Symbols dict
    symbols = state["symbols"]
    if not isinstance(symbols, dict) or len(symbols) == 0:
        raise ValueError("symbols must be a non-empty dict")

    for sym, sym_state in symbols.items():
        # 9a. Format check
        if not _is_valid_symbol_format(sym):
            raise ValueError(
                f"Invalid symbol key: {sym!r} (must end with -USDT)"
            )

        # 9b. Whitelist check (if provided)
        if allowed_symbols is not None and sym not in allowed_symbols:
            raise ValueError(
                f"Symbol {sym!r} not in allowed_symbols whitelist"
            )

        # 9c. Exact field match
        if not isinstance(sym_state, dict):
            raise ValueError(f"symbols[{sym}] must be a dict")
        actual_fields = set(sym_state.keys())
        if actual_fields != SYMBOL_STATE_FIELDS:
            missing = SYMBOL_STATE_FIELDS - actual_fields
            extra = actual_fields - SYMBOL_STATE_FIELDS
            raise ValueError(
                f"symbols[{sym}] fields mismatch. "
                f"Missing: {sorted(missing)}, Extra: {sorted(extra)}"
            )

        # 9d. Type checks for scalar timestamp fields
        for ts_field in ("last_processed_candle_ts", "cooldown_until_index", "last_funding_ts"):
            v = sym_state[ts_field]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ValueError(f"symbols[{sym}].{ts_field} must be None or int")

        # 9e. Type checks for dict fields
        if sym_state["pending_entry"] is not None and not isinstance(sym_state["pending_entry"], dict):
            raise ValueError(f"symbols[{sym}].pending_entry must be None or dict")
        if sym_state["position"] is not None and not isinstance(sym_state["position"], dict):
            raise ValueError(f"symbols[{sym}].position must be None or dict")

        # 9f. Per-symbol invariant: cannot have both position and pending_entry
        if sym_state["position"] is not None and sym_state["pending_entry"] is not None:
            raise ValueError(
                f"symbols[{sym}] cannot have both position and pending_entry"
            )

    # 10. Portfolio-level invariants
    open_positions = sum(1 for s in symbols.values() if s["position"] is not None)
    if open_positions > mcp:
        raise ValueError(
            f"Too many open positions: {open_positions} > max_concurrent_positions ({mcp})"
        )

    pending_count = sum(1 for s in symbols.values() if s["pending_entry"] is not None)
    if pending_count > 1:
        raise ValueError(
            f"Too many pending entries: {pending_count} (max 1 at portfolio level)"
        )

    # 11. Portfolio capacity: (open positions + pending entries) <= max_concurrent
    capacity_used = _portfolio_capacity_used(state)
    if capacity_used > mcp:
        raise ValueError(
            f"Portfolio capacity exceeded: {capacity_used} (positions + pending) > "
            f"max_concurrent_positions ({mcp})"
        )


def _portfolio_capacity_used(state: dict) -> int:
    """Count symbols that are 'in use' (position or pending_entry)."""
    return sum(
        1 for s in state["symbols"].values()
        if s["position"] is not None or s["pending_entry"] is not None
    )


# ============================================================
# Path helpers
# ============================================================

def _get_state_path_v2(path: str) -> str:
    if path.endswith(".json"):
        return path
    return os.path.join(path, "state.json")


# ============================================================
# Save (atomic + unique backup)
# ============================================================

def save_state_v2(path: str, state: dict) -> None:
    """
    Atomic write with unique backup name.

    - Validates state first (fail before any I/O).
    - Backup name includes UUID to prevent collisions in rapid succession.
    - Atomic replace via tempfile + os.replace.
    - Backup failure does NOT abort the save (best-effort), but is logged.
    """
    validate_state_v2(state)  # check_hash=False by default for save

    state["updated_at_utc"] = _utc_now_iso()

    target = _get_state_path_v2(path)
    state_dir = os.path.dirname(target) or "."
    os.makedirs(state_dir, exist_ok=True)

    # Backup existing state (best-effort, unique name)
    if os.path.exists(target):
        backup_name = (
            f"state.json.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
            f".{uuid.uuid4().hex[:8]}"
        )
        backup_path = os.path.join(state_dir, backup_name)
        try:
            with open(target, "r", encoding="utf-8") as src:
                content = src.read()
            with open(backup_path, "w", encoding="utf-8") as dst:
                dst.write(content)
        except OSError as e:
            # Log but continue. The atomic replace below will still work.
            import logging
            logging.getLogger(__name__).warning(f"Backup failed: {e}")

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".state_v2_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


# ============================================================
# Load
# ============================================================

def load_state_v2(
    path: str,
    strict: bool = True,
    allowed_symbols: list = None,
) -> dict:
    """
    Load V2 state.

    Args:
        path: Directory or .json file path.
        strict: If True, verify strategy_hash matches current computed hash.
                If False, skip hash check (for admin/repair scenarios).
        allowed_symbols: Optional whitelist for symbol validation.
    """
    target = _get_state_path_v2(path)
    if not os.path.exists(target):
        raise FileNotFoundError(f"V2 state not found at {target}")

    with open(target, "r", encoding="utf-8") as f:
        state = json.load(f)

    validate_state_v2(
        state,
        allowed_symbols=allowed_symbols,
        check_hash=strict,
    )

    return state


def load_or_init_state_v2(
    path: str,
    allowed_symbols: list = None,
    **kwargs,
) -> dict:
    """Load existing V2 state, or create a new one if absent."""
    target = _get_state_path_v2(path)
    if os.path.exists(target):
        return load_state_v2(path, strict=True, allowed_symbols=allowed_symbols)

    state = default_state_v2(**kwargs)
    save_state_v2(path, state)
    return state


def state_exists_v2(path: str) -> bool:
    return os.path.exists(_get_state_path_v2(path))


# ============================================================
# NOTE: delete_state_v2 is NOT in the normal API.
#
# If you need to delete V2 state (e.g., reset for testing), use the CLI:
#
#     python -m scripts.state_manager_v2_admin delete <path>
#
# Or manually delete files in multi_symbol_state_dir/.
#
# This module will NEVER delete state as a side effect of normal operations.
# ============================================================