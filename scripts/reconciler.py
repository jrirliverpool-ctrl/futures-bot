import time
import math
from data_feed import fetch_klines, INTERVAL_MS
from state_manager import load_state
from compute_hash import compute_strategy_hash

MIN_KLINES = 250
GAP_WARNING_THRESHOLD = 2
STALE_DATA_HOURS = 4

def _validate_klines_structure(klines):
    if not isinstance(klines, list): return "not a list"
    if not klines: return None
    prev_ts = None
    for i, k in enumerate(klines):
        if not isinstance(k, (list, tuple)) or len(k) < 5: return f"candle[{i}] invalid structure"
        ts = k[0]
        if isinstance(ts, bool) or not isinstance(ts, int): return f"candle[{i}] timestamp not int"
        if prev_ts is not None and ts <= prev_ts: return f"candle[{i}] not strictly ascending"
        prev_ts = ts
        try:
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        except (ValueError, TypeError):
            return f"candle[{i}] OHLC not numeric"
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            return f"candle[{i}] OHLC invalid values"
        if h < max(o, c) or l > min(o, c) or h < l:
            return f"candle[{i}] logical OHLC violation"
    return None

def reconcile_with_data(state, klines_data, interval, now_ms):
    checks = []
    def add_check(name, ok, detail, level="error"):
        checks.append({"name": name, "ok": ok, "level": level, "detail": detail})

    current_hash = compute_strategy_hash()
    state_hash = state.get("strategy_hash")
    if not state_hash: add_check("hash_match", False, "Missing")
    elif state_hash != current_hash: add_check("hash_match", False, "Mismatch")
    else: add_check("hash_match", True, "OK")

    if state.get("halt_trading"): add_check("halt_trading", False, "Halted")
    if state.get("order_status") == "UNKNOWN": add_check("unknown_order", False, "UNKNOWN order")
    if state.get("pending_order_id"): add_check("pending_order_id", False, f"Stale: {state['pending_order_id']}")

    peak = state.get("peak_equity", 0); eq = state.get("equity", 0)
    if not (isinstance(peak, (int, float)) and not isinstance(peak, bool) and math.isfinite(peak) and
            isinstance(eq, (int, float)) and not isinstance(eq, bool) and math.isfinite(eq)):
        add_check("equity_peak", False, "equity/peak invalid")
    elif eq > peak: add_check("equity_peak", False, f"{eq} > {peak}")
    else: add_check("equity_peak", True, "OK")

    if interval not in INTERVAL_MS:
        add_check("interval", False, f"Unknown: {interval!r}")
        return _aggregate(checks)
    add_check("interval", True, interval)

    symbols_to_check = []
    if isinstance(klines_data, dict):
        symbols_to_check = list(klines_data.items())
        if isinstance(state.get("symbols"), dict):
            missing = set(state["symbols"].keys()) - set(klines_data.keys())
            if missing: add_check("symbol_completeness", False, f"Missing: {sorted(missing)}")
            else: add_check("symbol_completeness", True, "All covered")
    else:
        symbols_to_check = [("DEFAULT", klines_data)]

    for sym, klines in symbols_to_check:
        if not klines:
            add_check("klines_sufficient", False, "No klines", level="warning")
            continue

        struct_err = _validate_klines_structure(klines)
        if struct_err:
            add_check("klines_structure", False, struct_err)
            continue
        add_check("klines_structure", True, "OK")

        n = len(klines)
        if n < MIN_KLINES: add_check("klines_sufficient", False, f"Only {n}", level="warning")
        else: add_check("klines_sufficient", True, f"{n}")

        interval_ms = INTERVAL_MS[interval]
        last_ts = klines[-1][0]

        close_time = last_ts + interval_ms
        if last_ts > now_ms: add_check("last_candle_closed", False, "Future candle")
        elif close_time <= now_ms: add_check("last_candle_closed", True, "OK")
        else: add_check("last_candle_closed", False, "Candle open")

        if isinstance(state.get("symbols"), dict) and sym in state["symbols"]:
            lpct = state["symbols"][sym].get("last_processed_candle_ts")
        else:
            lpct = state.get("last_processed_candle_ts")

        if lpct is None:
            add_check("last_processed_vs_latest", False, "lpct is None", level="warning")
        elif lpct > last_ts:
            add_check("last_processed_vs_latest", False, "lpct > last_ts")
        else:
            add_check("last_processed_vs_latest", True, "OK")
            missing = (last_ts - lpct) // interval_ms - 1
            if missing > GAP_WARNING_THRESHOLD: add_check("no_missing_candles", False, f"Missing {missing}", level="warning")
            else: add_check("no_missing_candles", True, "OK")

    return _aggregate(checks)

def _aggregate(checks):
    has_error = any(not c["ok"] and c["level"] == "error" for c in checks)
    has_warn = any(not c["ok"] and c["level"] == "warning" for c in checks)
    status = "ERROR" if has_error else ("WARNING" if has_warn else "OK")
    summary = f"{len(checks)} checks: {sum(1 for c in checks if not c['ok'])} failed"
    return {"status": status, "checks": checks, "summary": summary}

def reconcile_public(state_dir, symbol, interval="1h"):
    try:
        state = load_state(state_dir, strict=False)
    except Exception as e:
        return {"status": "ERROR", "summary": f"Load failed: {e}", "checks": []}
    try:
        klines = fetch_klines(symbol, interval, limit=300)
    except Exception:
        klines = None
    return reconcile_with_data(state, klines, interval, int(time.time() * 1000))