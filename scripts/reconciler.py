
import math
import time

from data_feed import fetch_klines, INTERVAL_MS
from state_manager import load_state
from compute_hash import compute_strategy_hash


MIN_KLINES = 250
GAP_WARNING_THRESHOLD = 2
STALE_DATA_HOURS = 4


def _is_finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_valid_timestamp(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _validate_klines_structure(klines):
    if not isinstance(klines, list):
        return "not a list"

    previous_ts = None

    for i, candle in enumerate(klines):
        if not isinstance(candle, (list, tuple)):
            return f"candle[{i}] invalid type"

        if len(candle) < 5:
            return f"candle[{i}] has fewer than 5 fields"

        ts = candle[0]
        if not _is_valid_timestamp(ts):
            return f"candle[{i}] timestamp invalid"

        if previous_ts is not None and ts <= previous_ts:
            return f"candle[{i}] not strictly ascending"

        previous_ts = ts

        try:
            o, h, l, c = map(float, candle[1:5])
        except (ValueError, TypeError, OverflowError):
            return f"candle[{i}] OHLC not numeric"

        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            return f"candle[{i}] OHLC invalid values"

        if h < max(o, c):
            return f"candle[{i}] high below open/close"

        if l > min(o, c):
            return f"candle[{i}] low above open/close"

        if h < l:
            return f"candle[{i}] high below low"

    return None


def _aggregate(checks):
    has_error = any(
        not c["ok"] and c["level"] == "error"
        for c in checks
    )
    has_warning = any(
        not c["ok"] and c["level"] == "warning"
        for c in checks
    )

    if has_error:
        status = "ERROR"
    elif has_warning:
        status = "WARNING"
    else:
        status = "OK"

    failed = sum(1 for c in checks if not c["ok"])

    return {
        "status": status,
        "checks": checks,
        "summary": f"{len(checks)} checks: {failed} failed",
    }


def reconcile_with_data(state, klines_data, interval, now_ms):
    """Pure, deterministic, read-only reconciliation.

    WARNING is not proof that market data or exchange state is safe.
    ERROR must stop processing.
    """
    checks = []

    def add_check(name, ok, detail, level="error"):
        checks.append({
            "name": name,
            "ok": bool(ok),
            "level": level,
            "detail": str(detail),
        })

    if not isinstance(state, dict):
        add_check("state_is_dict", False, "State is not a dictionary")
        return _aggregate(checks)

    add_check("state_is_dict", True, "OK")

    if not _is_valid_timestamp(now_ms):
        add_check("now_ms_valid", False, f"Invalid now_ms: {now_ms!r}")
        return _aggregate(checks)

    add_check("now_ms_valid", True, "OK")

    if interval not in INTERVAL_MS:
        add_check("interval", False, f"Unknown interval: {interval!r}")
        return _aggregate(checks)

    interval_ms = INTERVAL_MS[interval]
    add_check("interval", True, interval)

    # Strategy identity
    try:
        current_hash = compute_strategy_hash()
    except Exception as exc:
        current_hash = None
        add_check("hash_match", False, f"Hash computation failed: {exc}")
    else:
        state_hash = state.get("strategy_hash")
        add_check(
            "hash_match",
            isinstance(state_hash, str)
            and bool(state_hash)
            and state_hash == current_hash,
            (
                "OK"
                if state_hash == current_hash
                else "Missing, invalid, or mismatched strategy hash"
            ),
        )

    # Trading safety state
    if state.get("halt_trading"):
        add_check("halt_trading", False, "Trading halt is active")

    if state.get("circuit_breaker_active"):
        add_check(
            "circuit_breaker",
            False,
            "Circuit breaker is active",
        )

    order_status = state.get("order_status")
    if order_status in ("UNKNOWN", "PENDING_FILL"):
        add_check(
            "order_status",
            False,
            f"Unresolved order status: {order_status}",
        )

    if state.get("pending_order_id") is not None:
        add_check(
            "pending_order_id",
            False,
            "Unresolved pending exchange order",
        )

    # Equity checks
    equity = state.get("equity")
    peak = state.get("peak_equity")
    initial = state.get("initial_equity")

    equity_ok = _is_finite_number(equity) and equity >= 0
    peak_ok = _is_finite_number(peak) and peak >= 0

    add_check("equity_valid", equity_ok, f"equity={equity!r}")
    add_check("peak_equity_valid", peak_ok, f"peak_equity={peak!r}")

    if equity_ok and peak_ok:
        add_check(
            "equity_peak",
            equity <= peak,
            f"equity={equity}, peak_equity={peak}",
        )

    if initial is not None:
        add_check(
            "initial_equity_valid",
            _is_finite_number(initial) and initial > 0,
            f"initial_equity={initial!r}",
        )

    # Single-symbol or multi-symbol input
    state_symbols = state.get("symbols")

    if isinstance(state_symbols, dict):
        if not isinstance(klines_data, dict):
            add_check(
                "symbol_data_shape",
                False,
                "Multi-symbol state requires a klines dictionary",
            )
            return _aggregate(checks)

        symbols_to_check = list(state_symbols.items())

        missing = set(state_symbols) - set(klines_data)
        add_check(
            "symbol_completeness",
            not missing,
            f"Missing symbols: {sorted(missing)}" if missing else "All symbols covered",
        )
        multi = True
    else:
        if isinstance(klines_data, dict):
            add_check(
                "symbol_data_shape",
                False,
                "Single-symbol state requires a candle list",
            )
            return _aggregate(checks)

        symbols_to_check = [("DEFAULT", state)]
        multi = False

    def check_name(symbol, suffix):
        return f"{symbol}_{suffix}" if multi else suffix

    for symbol, symbol_state in symbols_to_check:
        if not isinstance(symbol_state, dict):
            add_check(
                check_name(symbol, "state_valid"),
                False,
                "Symbol state is not a dictionary",
            )
            continue

        klines = (
            klines_data.get(symbol)
            if multi
            else klines_data
        )

        active = (
            symbol_state.get("position") is not None
            or symbol_state.get("pending_entry") is not None
        )
        missing_level = "error" if active else "warning"

        # Candle availability
        if not isinstance(klines, list) or not klines:
            add_check(
                check_name(symbol, "klines_available"),
                False,
                "No usable klines",
                missing_level,
            )
            add_check(
                check_name(symbol, "klines_sufficient"),
                False,
                "No candles available",
                missing_level,
            )
            add_check(
                check_name(symbol, "last_candle_closed"),
                False,
                "Cannot verify candle close without data",
                missing_level,
            )
            add_check(
                check_name(symbol, "last_processed_vs_latest"),
                False,
                "Cannot compare without candle data",
                missing_level,
            )
            add_check(
                check_name(symbol, "no_missing_candles"),
                False,
                "Cannot verify candle gaps without data",
                missing_level,
            )
            continue

        add_check(
            check_name(symbol, "klines_available"),
            True,
            f"{len(klines)} candles",
        )

        structure_error = _validate_klines_structure(klines)
        if structure_error:
            add_check(
                check_name(symbol, "klines_structure"),
                False,
                structure_error,
            )
            continue

        add_check(
            check_name(symbol, "klines_structure"),
            True,
            "OK",
        )

        # Candle sufficiency
        candle_count = len(klines)
        sufficient = candle_count >= MIN_KLINES

        add_check(
            check_name(symbol, "klines_sufficient"),
            sufficient,
            f"{candle_count}; minimum={MIN_KLINES}",
            "error" if active else "warning",
        )

        last_ts = klines[-1][0]

        if last_ts > now_ms:
            add_check(
                check_name(symbol, "last_candle_not_future"),
                False,
                "Latest candle timestamp is in the future",
            )
            add_check(
                check_name(symbol, "last_candle_closed"),
                False,
                "Latest candle timestamp is in the future",
            )
            continue

        add_check(
            check_name(symbol, "last_candle_not_future"),
            True,
            "OK",
        )

        # Candle timestamps are OPEN timestamps.
        close_time = last_ts + interval_ms
        closed = close_time <= now_ms

        add_check(
            check_name(symbol, "last_candle_closed"),
            closed,
            "Closed" if closed else "Latest candle is still open",
        )

        if not closed:
            continue

        # Data freshness
        age_ms = now_ms - close_time
        stale_limit = STALE_DATA_HOURS * 3_600_000
        fresh = age_ms <= stale_limit

        add_check(
            check_name(symbol, "data_freshness"),
            fresh,
            f"Age {age_ms / 3_600_000:.2f} hours",
            "error" if active else "warning",
        )

        # Last processed candle
        lpct = symbol_state.get("last_processed_candle_ts")

        if lpct is None:
            add_check(
                check_name(symbol, "last_processed_vs_latest"),
                False,
                "last_processed_candle_ts is None",
                missing_level,
            )
            add_check(
                check_name(symbol, "no_missing_candles"),
                False,
                "Cannot calculate gap without last processed timestamp",
                missing_level,
            )
            continue

        if not _is_valid_timestamp(lpct):
            add_check(
                check_name(symbol, "last_processed_vs_latest"),
                False,
                f"Invalid last processed timestamp: {lpct!r}",
            )
            continue

        if lpct > last_ts:
            add_check(
                check_name(symbol, "last_processed_vs_latest"),
                False,
                "Last processed timestamp is ahead of latest candle",
            )
            continue

        add_check(
            check_name(symbol, "last_processed_vs_latest"),
            True,
            "OK",
        )

        # Missing candle detection
        delta = last_ts - lpct

        if delta % interval_ms != 0:
            add_check(
                check_name(symbol, "no_missing_candles"),
                False,
                "Timestamp difference is not aligned to interval",
            )
            continue

        missing_candles = (delta // interval_ms) - 1
        missing_candles = max(0, missing_candles)

        gap_ok = missing_candles < GAP_WARNING_THRESHOLD

        add_check(
            check_name(symbol, "no_missing_candles"),
            gap_ok,
            f"Missing {missing_candles} candles",
            "warning",
        )

    return _aggregate(checks)


def reconcile_public(state_dir, symbol, interval="1h"):
    try:
        state = load_state(state_dir, strict=False)
    except Exception as exc:
        return {
            "status": "ERROR",
            "summary": f"Load failed: {exc}",
            "checks": [],
        }

    try:
        klines = fetch_klines(symbol, interval, limit=300)
    except Exception as exc:
        return {
            "status": "ERROR",
            "summary": f"Klines fetch failed: {exc}",
            "checks": [],
        }

    return reconcile_with_data(
        state,
        klines,
        interval,
        int(time.time() * 1000),
    )
