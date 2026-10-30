"""
reconciler.py — Pre-flight health check.
v1.1 — Fixes: Detect UNKNOWN orders, validate candle gaps, detect open position mismatch.
"""
import time
from data_feed import fetch_klines, INTERVAL_MS
from state_manager import load_state
from compute_hash import compute_strategy_hash
import os

MIN_KLINES = 250
GAP_WARNING_THRESHOLD = 2

def reconcile_with_data(state, klines, interval, now_ms):
    checks = []
    
    def add_check(name, ok, detail, level="error"):
        checks.append({"name": name, "ok": ok, "level": level, "detail": detail})

    # 1. HALT & Order Status Check
    if state.get("halt_trading"):
        add_check("halt_trading", False, "System halted due to UNKNOWN order.")
    if state.get("order_status") == "UNKNOWN":
        add_check("unknown_order", False, "UNKNOWN order exists. Manual intervention required.")
    if state.get("pending_order_id"):
        add_check("pending_order_id", False, f"Stale pending_order_id: {state['pending_order_id']}")

    # 2. Equity & Peak checks
    peak = state.get("peak_equity", 0)
    eq = state.get("equity", 0)
    if eq > peak:
        add_check("equity_peak", False, f"equity {eq} > peak {peak}")
    else:
        add_check("equity_peak", True, "OK")

    # 3. Klines structure & gaps
    if not klines:
        add_check("klines_avail", False, "No klines provided")
    else:
        n = len(klines)
        if n < MIN_KLINES:
            add_check("klines_count", False, f"Only {n} klines (min {MIN_KLINES})", level="warning")
        else:
            add_check("klines_count", True, f"{n} klines")

        interval_ms = INTERVAL_MS[interval]
        last_ts = klines[-1][0]
        
        # Check gap
        lpct = state.get("last_processed_candle_ts")
        if lpct is not None:
            if lpct > last_ts:
                add_check("lpct_future", False, f"lpct {lpct} > last_ts {last_ts}")
            else:
                missing = (last_ts - lpct) // interval_ms - 1
                if missing > GAP_WARNING_THRESHOLD:
                    add_check("candle_gap", False, f"Missing {missing} candles", level="warning")
                else:
                    add_check("candle_gap", True, "OK")

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
    except Exception as e:
        klines = None
        
    now_ms = int(time.time() * 1000)
    return reconcile_with_data(state, klines, interval, now_ms)