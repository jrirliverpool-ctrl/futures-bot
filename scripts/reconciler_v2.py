"""reconciler_v2.py — Multi-symbol health check for ApexQuant V2.

Read-only. Never modifies state, never places orders, never touches files.

Layers:
  1. reconcile_portfolio(state_v2)           — pure, no klines needed
  2. reconcile_with_data_v2(state_v2, klines_by_symbol, interval, now_ms)
                                              — pure, deterministic
  3. reconcile_public_v2(state_dir, symbols, interval)
                                              — fetches klines, wrapper

Status semantics:
  - OK      : no errors, no warnings. Safe to proceed.
  - WARNING : no errors, but something suspicious (idle symbol without
              data, gap, missing CB reason). Caller decides — likely OK
              to proceed with logging.
  - ERROR   : something dangerous (active symbol without data, portfolio
              invariant broken, malformed state/klines). Caller MUST halt
              the trading cycle. See docs/v2_multi_symbol_design.md §5.

Design notes:
  - All checks are wrapped in try/except → exceptions become ERROR checks,
    never crash. Callers can always read result["status"].
  - Capacity check counts each symbol once (position OR pending — both
    violate the exclusive invariant and are reported separately).
  - Peak/equity: math.isfinite is required; NaN or Inf → ERROR.
"""

import math
import time

from data_feed import fetch_klines, INTERVAL_MS
from state_manager_v2 import load_state_v2


MIN_KLINES = 250
GAP_WARNING_THRESHOLD = 2

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_ERROR = "ERROR"


# ============================================================
# Result helpers
# ============================================================

def _result(status, checks, summary):
    return {"status": status, "checks": checks, "summary": summary}


def _check(name, ok, detail, level="error"):
    return {"name": name, "ok": ok, "level": level, "detail": detail}


def _aggregate(checks):
    has_error = any(not c["ok"] and c["level"] == "error" for c in checks)
    has_warning = any(not c["ok"] and c["level"] == "warning" for c in checks)
    if has_error:
        return STATUS_ERROR
    if has_warning:
        return STATUS_WARNING
    return STATUS_OK


def _summarize(checks):
    n_err = sum(1 for c in checks if not c["ok"] and c["level"] == "error")
    n_warn = sum(1 for c in checks if not c["ok"] and c["level"] == "warning")
    return f"{len(checks)} checks: {n_err} error, {n_warn} warning"


def _is_finite_number(v):
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
    )


def _validate_klines_structure(klines):
    """Return error string if malformed, else None.

    Contract: klines is a list of [ts_int, o, h, l, c, ...] with
    strictly increasing integer timestamps.
    """
    if not isinstance(klines, list):
        return f"klines not a list (got {type(klines).__name__})"

    if not klines:
        return None  # empty is legal, handled separately

    prev_ts = None
    for i, c in enumerate(klines):
        if not isinstance(c, (list, tuple)):
            return f"candle[{i}] not a list/tuple (got {type(c).__name__})"
        if len(c) < 5:
            return f"candle[{i}] has fewer than 5 elements ({len(c)})"
        ts = c[0]
        if isinstance(ts, bool) or not isinstance(ts, int):
            return f"candle[{i}].timestamp not int (got {type(ts).__name__})"
        if prev_ts is not None and ts <= prev_ts:
            return (
                f"candles not strictly ascending: "
                f"candle[{i}].ts={ts} <= prev={prev_ts}"
            )
        prev_ts = ts
    return None


# ============================================================
# Portfolio checks (pure)
# ============================================================

def reconcile_portfolio(state_v2):
    """Portfolio-level invariants. Wrapped in try/except for safety."""
    checks = []

    try:
        if not isinstance(state_v2, dict):
            return _result(
                STATUS_ERROR,
                [_check("portfolio_state_is_dict", False,
                        f"got {type(state_v2).__name__}")],
                "state is not a dict",
            )
        checks.append(_check("portfolio_state_is_dict", True, "dict"))

        # Strategy hash
        sh = state_v2.get("strategy_hash")
        checks.append(_check(
            "portfolio_strategy_hash_present",
            isinstance(sh, str) and len(sh) > 0,
            f"strategy_hash={'present' if sh else 'missing'}",
        ))

        # Equity: number, finite, >= 0
        equity = state_v2.get("equity")
        if not _is_finite_number(equity):
            checks.append(_check(
                "portfolio_equity_finite_non_negative",
                False,
                f"equity={equity!r} (must be finite number)",
            ))
        elif equity < 0:
            checks.append(_check(
                "portfolio_equity_finite_non_negative",
                False,
                f"equity={equity} < 0",
            ))
        else:
            checks.append(_check(
                "portfolio_equity_finite_non_negative",
                True,
                f"equity={equity}",
            ))

        # Peak equity: finite, >= initial
        peak = state_v2.get("peak_equity")
        initial = state_v2.get("initial_equity")
        if not _is_finite_number(peak):
            checks.append(_check(
                "portfolio_peak_geq_initial",
                False,
                f"peak_equity={peak!r} (must be finite number)",
            ))
        elif not _is_finite_number(initial):
            checks.append(_check(
                "portfolio_peak_geq_initial",
                False,
                f"initial_equity={initial!r} (must be finite number)",
            ))
        elif peak < initial:
            checks.append(_check(
                "portfolio_peak_geq_initial",
                False,
                f"peak={peak} < initial={initial}",
            ))
        else:
            checks.append(_check(
                "portfolio_peak_geq_initial",
                True,
                f"peak={peak} initial={initial}",
            ))

        # Symbols dict
        symbols = state_v2.get("symbols")
        if not isinstance(symbols, dict):
            checks.append(_check(
                "portfolio_symbols_is_dict",
                False,
                f"got {type(symbols).__name__}",
            ))
            return _result(_aggregate(checks), checks, _summarize(checks))
        if not symbols:
            checks.append(_check(
                "portfolio_symbols_non_empty",
                False,
                "empty symbols",
            ))
            return _result(_aggregate(checks), checks, _summarize(checks))

        checks.append(_check(
            "portfolio_symbols_non_empty",
            True,
            f"{len(symbols)} symbols",
        ))

        # max_concurrent_positions
        mcp = state_v2.get("max_concurrent_positions")
        if isinstance(mcp, bool) or not isinstance(mcp, int) or mcp < 1:
            checks.append(_check(
                "portfolio_max_concurrent_valid",
                False,
                f"max_concurrent_positions={mcp!r} (must be int >= 1)",
            ))
            # Cannot compute capacity, stop here.
            return _result(_aggregate(checks), checks, _summarize(checks))
        checks.append(_check(
            "portfolio_max_concurrent_valid",
            True,
            f"max_concurrent={mcp}",
        ))

        # Per-symbol structural + exclusive-state checks + capacity count
        bad_structure = []
        exclusive_violations = []
        reserved = 0  # each symbol counts once if it has position OR pending

        for sym, s in symbols.items():
            if not isinstance(sym, str) or not sym.endswith("-SWAP-USDT"):
                bad_structure.append(f"bad key: {sym!r}")
                continue
            if not isinstance(s, dict):
                bad_structure.append(f"{sym}: state not dict")
                continue
            pos = s.get("position")
            pend = s.get("pending_entry")
            if pos is not None and not isinstance(pos, dict):
                bad_structure.append(f"{sym}: position not dict")
            if pend is not None and not isinstance(pend, dict):
                bad_structure.append(f"{sym}: pending_entry not dict")
            if pos is not None and pend is not None:
                exclusive_violations.append(sym)
            if pos is not None or pend is not None:
                reserved += 1

        checks.append(_check(
            "portfolio_symbols_structure",
            len(bad_structure) == 0,
            "all symbol states valid"
            if not bad_structure
            else f"invalid: {bad_structure[:5]}",
        ))

        checks.append(_check(
            "portfolio_symbols_exclusive_state",
            len(exclusive_violations) == 0,
            "all exclusive"
            if not exclusive_violations
            else f"both position+pending: {exclusive_violations[:5]}",
        ))

        # Capacity derived from max_concurrent_positions
        checks.append(_check(
            "portfolio_capacity_within_limit",
            reserved <= mcp,
            f"reserved={reserved} max={mcp}",
        ))

        # Circuit breaker: reason present when active
        cb_active = state_v2.get("circuit_breaker_active", False)
        cb_reason = state_v2.get("circuit_breaker_reason")
        if cb_active:
            reason_ok = isinstance(cb_reason, str) and len(cb_reason) > 0
            checks.append(_check(
                "portfolio_cb_reason_when_active",
                reason_ok,
                f"cb_active=True reason={cb_reason!r}",
                level="error" if not reason_ok else "error",
            ))
        else:
            checks.append(_check(
                "portfolio_cb_reason_when_active",
                True,
                "cb inactive",
            ))

    except Exception as e:
        checks.append(_check(
            "portfolio_exception",
            False,
            f"unexpected: {type(e).__name__}: {e}",
        ))

    return _result(_aggregate(checks), checks, _summarize(checks))


# ============================================================
# Per-symbol data checks (pure)
# ============================================================

def _check_symbol_data(symbol, sym_state, klines, interval, now_ms):
    """Return list of checks for one symbol's market data.

    Order of checks:
      1. interval known (before anything else)
      2. klines available + structural validity
      3. klines sufficient (level depends on active/idle)
      4. last candle closed
      5. last_processed present + valid + not future
      6. gap
    """
    checks = []
    prefix = f"symbol_{symbol}"

    has_position = False
    has_pending = False
    if isinstance(sym_state, dict):
        has_position = sym_state.get("position") is not None
        has_pending = sym_state.get("pending_entry") is not None
    is_active = has_position or has_pending

    # 1. interval must be known regardless of klines
    if interval not in INTERVAL_MS:
        checks.append(_check(
            f"{prefix}_interval_known",
            False,
            f"unknown interval: {interval!r}",
        ))
        return checks
    checks.append(_check(
        f"{prefix}_interval_known", True, f"interval={interval}"
    ))

    # 2. klines available
    if klines is None:
        level = "error" if is_active else "warning"
        checks.append(_check(
            f"{prefix}_klines_available",
            False,
            "no klines provided",
            level=level,
        ))
        return checks
    checks.append(_check(
        f"{prefix}_klines_available", True, "provided"
    ))

    # 2b. structural validity
    struct_err = _validate_klines_structure(klines)
    if struct_err:
        checks.append(_check(
            f"{prefix}_klines_structure",
            False,
            f"malformed: {struct_err}",
        ))
        return checks
    checks.append(_check(
        f"{prefix}_klines_structure", True, "ok"
    ))

    # 3. sufficient klines
    n = len(klines)
    klines_ok = n >= MIN_KLINES
    checks.append(_check(
        f"{prefix}_klines_sufficient",
        klines_ok,
        f"{n} klines (min={MIN_KLINES})"
        + (" [active]" if is_active else ""),
        level="error" if is_active else "warning",
    ))
    if not klines_ok:
        return checks

    interval_ms = INTERVAL_MS[interval]
    last_ts = klines[-1][0]

    # 4. last candle closed
    close_time = last_ts + interval_ms
    checks.append(_check(
        f"{prefix}_last_candle_closed",
        close_time <= now_ms,
        f"last_ts={last_ts} close_time={close_time} now={now_ms}",
    ))

    # 5. last_processed_candle_ts
    lpct = sym_state.get("last_processed_candle_ts") if isinstance(sym_state, dict) else None

    if lpct is None:
        level = "error" if is_active else "warning"
        checks.append(_check(
            f"{prefix}_last_processed_present",
            False,
            "last_processed_candle_ts is None",
            level=level,
        ))
        return checks

    if isinstance(lpct, bool) or not isinstance(lpct, int):
        checks.append(_check(
            f"{prefix}_last_processed_int",
            False,
            f"not int (got {type(lpct).__name__})",
        ))
        return checks
    checks.append(_check(
        f"{prefix}_last_processed_int", True, f"lpct={lpct}"
    ))

    if lpct > last_ts:
        checks.append(_check(
            f"{prefix}_last_processed_not_future",
            False,
            f"lpct={lpct} > last_candle={last_ts}",
        ))
        return checks
    checks.append(_check(
        f"{prefix}_last_processed_not_future",
        True,
        f"lpct={lpct} last_candle={last_ts}",
    ))

    # 6. gap
    delta = last_ts - lpct
    missing = max(0, (delta // interval_ms) - 1)
    checks.append(_check(
        f"{prefix}_no_significant_gap",
        missing < GAP_WARNING_THRESHOLD,
        f"missing_candles={missing} (threshold={GAP_WARNING_THRESHOLD})",
        level="warning",
    ))

    return checks


# ============================================================
# Pure with-data
# ============================================================

def reconcile_with_data_v2(state_v2, klines_by_symbol, interval, now_ms):
    """Full reconcile using provided klines. Pure, no I/O. Never mutates inputs.

    Args:
        state_v2: dict (V2 state, read-only).
        klines_by_symbol: dict {symbol: list_of_klines or None}.
            Symbols missing → treated as None.
        interval: e.g. "1h".
        now_ms: current timestamp (deterministic).

    Returns:
        {"status", "checks", "summary"}
    """
    portfolio = reconcile_portfolio(state_v2)
    all_checks = list(portfolio["checks"])

    symbols = state_v2.get("symbols", {}) if isinstance(state_v2, dict) else {}
    if not isinstance(symbols, dict):
        # Structure error already reported; don't iterate.
        return _result(_aggregate(all_checks), all_checks, _summarize(all_checks))

    for sym, sym_state in symbols.items():
        klines = klines_by_symbol.get(sym) if isinstance(klines_by_symbol, dict) else None
        try:
            all_checks.extend(
                _check_symbol_data(sym, sym_state, klines, interval, now_ms)
            )
        except Exception as e:
            all_checks.append(_check(
                f"symbol_{sym}_exception",
                False,
                f"unexpected: {type(e).__name__}: {e}",
            ))

    return _result(_aggregate(all_checks), all_checks, _summarize(all_checks))


# ============================================================
# Public wrapper (fetches klines, never mutates state)
# ============================================================

def reconcile_public_v2(state_dir, symbols, interval="1h"):
    """Wrapper: load V2 state, fetch klines per symbol, run full reconcile.

    Read-only. Any error is converted into a check, never raised.
    """
    checks = []

    # Load state
    try:
        state_v2 = load_state_v2(state_dir)
        checks.append(_check("state_loads_v2", True, "state.json loaded"))
    except Exception as e:
        checks.append(_check(
            "state_loads_v2", False,
            f"load failed: {type(e).__name__}: {e}",
        ))
        return _result(STATUS_ERROR, checks, _summarize(checks))

    # Fetch klines per symbol
    klines_by_symbol = {}
    for sym in symbols:
        try:
            klines = fetch_klines(sym, interval, limit=300)
            klines_by_symbol[sym] = klines
            checks.append(_check(
                f"fetch_{sym}", True, f"{len(klines)} klines"
            ))
        except Exception as e:
            klines_by_symbol[sym] = None
            checks.append(_check(
                f"fetch_{sym}", False,
                f"fetch failed: {type(e).__name__}: {e}",
                level="warning",
            ))

    # Pure checks
    now_ms = int(time.time() * 1000)
    try:
        inner = reconcile_with_data_v2(state_v2, klines_by_symbol, interval, now_ms)
        checks.extend(inner["checks"])
    except Exception as e:
        checks.append(_check(
            "reconcile_inner_exception", False,
            f"unexpected: {type(e).__name__}: {e}",
        ))

    return _result(_aggregate(checks), checks, _summarize(checks))