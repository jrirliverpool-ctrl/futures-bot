import time
import math

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
    """
    Validate candle structure, timestamps and OHLC relationships.

    Kline timestamp is candle OPEN time in milliseconds.
    """

    if not isinstance(klines, list):
        return "not a list"

    if not klines:
        return None

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
            o = float(candle[1])
            h = float(candle[2])
            l = float(candle[3])
            c = float(candle[4])
        except (ValueError, TypeError, OverflowError):
            return f"candle[{i}] OHLC not numeric"

        if not all(
            math.isfinite(x) and x > 0
            for x in (o, h, l, c)
        ):
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
        not check["ok"] and check["level"] == "error"
        for check in checks
    )

    has_warning = any(
        not check["ok"] and check["level"] == "warning"
        for check in checks
    )

    if has_error:
        status = "ERROR"
    elif has_warning:
        status = "WARNING"
    else:
        status = "OK"

    failed = sum(
        1 for check in checks
        if not check["ok"]
    )

    summary = f"{len(checks)} checks: {failed} failed"

    return {
        "status": status,
        "checks": checks,
        "summary": summary,
    }


def reconcile_with_data(state, klines_data, interval, now_ms):
    """
    Read-only safety reconciliation.

    This function:
    - Does not mutate state.
    - Does not modify klines.
    - Does not place orders.
    - Returns ERROR for dangerous inconsistencies.
    - Returns WARNING for non-critical issues.

    IMPORTANT:
    A caller must not interpret WARNING as proof that
    market data is fresh or that exchange state is reconciled.
    """

    checks = []

    def add_check(name, ok, detail, level="error"):
        checks.append({
            "name": name,
            "ok": bool(ok),
            "level": level,
            "detail": str(detail),
        })

    # ---------------------------------------------------------
    # 1. Input validation
    # ---------------------------------------------------------

    if not isinstance(state, dict):
        add_check(
            "state_is_dict",
            False,
            "State is not a dictionary",
        )
        return _aggregate(checks)

    add_check(
        "state_is_dict",
        True,
        "OK",
    )

    if not _is_valid_timestamp(now_ms):
        add_check(
            "now_ms_valid",
            False,
            f"Invalid now_ms: {now_ms!r}",
        )
        return _aggregate(checks)

    add_check(
        "now_ms_valid",
        True,
        "OK",
    )

    if interval not in INTERVAL_MS:
        add_check(
            "interval",
            False,
            f"Unknown interval: {interval!r}",
        )
        return _aggregate(checks)

    interval_ms = INTERVAL_MS[interval]

    add_check(
        "interval",
        True,
        interval,
    )

    # ---------------------------------------------------------
    # 2. Strategy identity
    # ---------------------------------------------------------

    try:
        current_hash = compute_strategy_hash()
    except Exception as exc:
        add_check(
            "hash_match",
            False,
            f"Could not compute current hash: {exc}",
        )
        current_hash = None

    state_hash = state.get("strategy_hash")

    if current_hash is None:
        pass

    elif not isinstance(state_hash, str) or not state_hash:
        add_check(
            "hash_match",
            False,
            "Missing or invalid strategy hash",
        )

    elif state_hash != current_hash:
        add_check(
            "hash_match",
            False,
            "Strategy hash mismatch",
        )

    else:
        add_check(
            "hash_match",
            True,
            "OK",
        )

    # ---------------------------------------------------------
    # 3. Trading halt and unresolved order state
    # ---------------------------------------------------------

    if state.get("halt_trading"):
        add_check(
            "halt_trading",
            False,
            "Trading halt is active",
        )

    order_status = state.get("order_status")

    if order_status in ("UNKNOWN", "PENDING_FILL"):
        add_check(
            "order_status",
            False,
            f"Unresolved order status: {order_status}",
        )

    pending_order_id = state.get("pending_order_id")

    if pending_order_id is not None:
        add_check(
            "pending_order_id",
            False,
            f"Unresolved pending order: {pending_order_id}",
        )

    # ---------------------------------------------------------
    # 4. Equity consistency
    # ---------------------------------------------------------

    equity = state.get("equity")
    peak_equity = state.get("peak_equity")
    initial_equity = state.get("initial_equity")

    equity_valid = _is_finite_number(equity) and equity >= 0
    peak_valid = (
        _is_finite_number(peak_equity)
        and peak_equity >= 0
    )

    if not equity_valid:
        add_check(
            "equity_valid",
            False,
            f"Invalid equity: {equity!r}",
        )
    else:
        add_check(
            "equity_valid",
            True,
            "OK",
        )

    if not peak_valid:
        add_check(
            "peak_equity_valid",
            False,
            f"Invalid peak equity: {peak_equity!r}",
        )
    else:
        add_check(
            "peak_equity_valid",
            True,
            "OK",
        )

    if equity_valid and peak_valid:

        if equity > peak_equity:
            add_check(
                "equity_peak",
                False,
                f"Equity {equity} exceeds peak {peak_equity}",
            )
        else:
            add_check(
                "equity_peak",
                True,
                "OK",
            )

    if initial_equity is not None:

        if (
            not _is_finite_number(initial_equity)
            or initial_equity <= 0
        ):
            add_check(
                "initial_equity_valid",
                False,
                f"Invalid initial equity: {initial_equity!r}",
            )
        else:
            add_check(
                "initial_equity_valid",
                True,
                "OK",
            )

    # ---------------------------------------------------------
    # 5. Determine symbols to reconcile
    # ---------------------------------------------------------

    state_symbols = state.get("symbols")

    if isinstance(state_symbols, dict):

        symbols_to_check = list(state_symbols.items())

        if not isinstance(klines_data, dict):
            add_check(
                "symbol_data_shape",
                False,
                "Multi-symbol state requires klines dictionary",
            )
            return _aggregate(checks)

        missing_symbols = (
            set(state_symbols.keys())
            - set(klines_data.keys())
        )

        if missing_symbols:
            add_check(
                "symbol_completeness",
                False,
                f"Missing symbols: {sorted(missing_symbols)}",
            )
        else:
            add_check(
                "symbol_completeness",
                True,
                "All symbols covered",
            )

    else:

        symbols_to_check = [
            ("DEFAULT", state)
        ]

        if isinstance(klines_data, dict):
            add_check(
                "symbol_data_shape",
                False,
                "Single-symbol state requires a candle list",
            )
            return _aggregate(checks)

    # ---------------------------------------------------------
    # 6. Per-symbol checks
    # ---------------------------------------------------------

    for symbol, symbol_state in symbols_to_check:

        if not isinstance(symbol_state, dict):
            add_check(
                f"{symbol}_state_valid",
                False,
                "Symbol state is not a dictionary",
            )
            continue

        if isinstance(state_symbols, dict):
            klines = klines_data.get(symbol)
        else:
            klines = klines_data

        active = (
            symbol_state.get("position") is not None
            or symbol_state.get("pending_entry") is not None
        )

        # -----------------------------------------------------
        # 6.1 Candle availability
        # -----------------------------------------------------

        if not isinstance(klines, list) or not klines:

            add_check(
                f"{symbol}_klines_available",
                False,
                "No usable klines",
                level="error" if active else "warning",
            )

            continue

        # -----------------------------------------------------
        # 6.2 Candle structure
        # -----------------------------------------------------

        structure_error = _validate_klines_structure(klines)

        if structure_error:

            add_check(
                f"{symbol}_klines_structure",
                False,
                structure_error,
            )

            continue

        add_check(
            f"{symbol}_klines_structure",
            True,
            "OK",
        )

        # -----------------------------------------------------
        # 6.3 Candle sufficiency
        # -----------------------------------------------------

        candle_count = len(klines)

        if candle_count < MIN_KLINES:

            add_check(
                f"{symbol}_klines_sufficient",
                False,
                f"Only {candle_count}; minimum is {MIN_KLINES}",
                level="error" if active else "warning",
            )

        else:

            add_check(
                f"{symbol}_klines_sufficient",
                True,
                str(candle_count),
            )

        # -----------------------------------------------------
        # 6.4 Last candle timestamp
        # -----------------------------------------------------

        last_ts = klines[-1][0]

        if last_ts > now_ms:

            add_check(
                f"{symbol}_last_candle_not_future",
                False,
                "Latest candle timestamp is in the future",
            )

            continue

        add_check(
            f"{symbol}_last_candle_not_future",
            True,
            "OK",
        )

        # Timestamp is candle OPEN time.
        close_time = last_ts + interval_ms

        if close_time > now_ms:

            add_check(
                f"{symbol}_last_candle_closed",
                False,
                "Latest candle is still open",
            )

            continue

        add_check(
            f"{symbol}_last_candle_closed",
            True,
            "OK",
        )

        # -----------------------------------------------------
        # 6.5 Data freshness
        # -----------------------------------------------------

        data_age_ms = now_ms - close_time
        stale_limit_ms = STALE_DATA_HOURS * 3_600_000

        if data_age_ms > stale_limit_ms:

            add_check(
                f"{symbol}_data_freshness",
                False,
                (
                    f"Last closed candle is stale: "
                    f"{data_age_ms / 3_600_000:.2f} hours old"
                ),
                level="error" if active else "warning",
            )

        else:

            add_check(
                f"{symbol}_data_freshness",
                True,
                f"Age {data_age_ms / 60_000:.1f} minutes",
            )

        # -----------------------------------------------------
        # 6.6 Last processed candle timestamp
        # -----------------------------------------------------

        lpct = symbol_state.get("last_processed_candle_ts")

        if lpct is None:

            add_check(
                f"{symbol}_last_processed_vs_latest",
                False,
                "last_processed_candle_ts is None",
                level="error" if active else "warning",
            )

            continue

        if not _is_valid_timestamp(lpct):

            add_check(
                f"{symbol}_last_processed_vs_latest",
                False,
                f"Invalid last_processed timestamp: {lpct!r}",
            )

            continue

        if lpct > last_ts:

            add_check(
                f"{symbol}_last_processed_vs_latest",
                False,
                "last_processed timestamp is ahead of latest candle",
            )

            continue

        add_check(
            f"{symbol}_last_processed_vs_latest",
            True,
            "OK",
        )

        # -----------------------------------------------------
        # 6.7 Missing candle gap
        # -----------------------------------------------------

        delta = last_ts - lpct

        if delta % interval_ms != 0:

            add_check(
                f"{symbol}_no_missing_candles",
                False,
                "Timestamp difference is not aligned to interval",
            )

            continue

        missing_candles = (
            delta // interval_ms
        ) - 1

        if missing_candles > GAP_WARNING_THRESHOLD:

            add_check(
                f"{symbol}_no_missing_candles",
                False,
                f"Missing {missing_candles} candles",
                level="warning",
            )

        else:

            add_check(
                f"{symbol}_no_missing_candles",
                True,
                f"Missing {missing_candles} candles",
            )

    return _aggregate(checks)


def reconcile_public(state_dir, symbol, interval="1h"):

    try:
        state = load_state(
            state_dir,
            strict=False,
        )

    except Exception as exc:

        return {
            "status": "ERROR",
            "summary": f"Load failed: {exc}",
            "checks": [],
        }

    try:

        klines = fetch_klines(
            symbol,
            interval,
            limit=300,
        )

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