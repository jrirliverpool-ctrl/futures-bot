"""
reconciler.py — Public Dry-Run Reconciliation.

هدف: بررسی سلامت داده‌ی عمومی و سازگاری state ذخیره‌شده.
"""

import os
import time

from data_feed import fetch_klines, INTERVAL_MS
from paper_state_manager import load_state, state_exists
from compute_hash import compute_strategy_hash


MIN_KLINES = 250
GAP_WARNING_THRESHOLD = 2

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_ERROR = "ERROR"


def _result(status: str, checks: list, summary: str) -> dict:
    return {"status": status, "checks": checks, "summary": summary}


def _check(name: str, ok: bool, detail: str, level: str = "error") -> dict:
    return {"name": name, "ok": ok, "level": level, "detail": detail}


def _aggregate_status(checks: list) -> str:
    has_error = any(not c["ok"] and c["level"] == "error" for c in checks)
    has_warning = any(not c["ok"] and c["level"] == "warning" for c in checks)
    if has_error: return STATUS_ERROR
    if has_warning: return STATUS_WARNING
    return STATUS_OK


def _summarize(checks: list) -> str:
    n_error = sum(1 for c in checks if not c["ok"] and c["level"] == "error")
    n_warn = sum(1 for c in checks if not c["ok"] and c["level"] == "warning")
    return f"{len(checks)} checks: {n_error} error, {n_warn} warning"


def _check_state_exists(state_dir: str) -> dict:
    return _check("state_exists", state_exists(state_dir), f"state.json in {state_dir}")


def _check_state_loads(state_dir: str) -> tuple:
    try:
        state = load_state(state_dir, strict=False)
        return _check("state_loads", True, "state.json loaded"), state
    except Exception as e:
        return _check("state_loads", False, f"load failed: {e}"), None


def _check_klines(klines: list) -> dict:
    n = len(klines)
    ok = n >= MIN_KLINES
    return _check("klines_sufficient", ok, f"received {n} klines (min={MIN_KLINES})", level="warning")


def _check_last_candle_closed(klines: list, interval: str, now_ms: int) -> dict:
    if not klines:
        return _check("last_candle_closed", False, "no klines to check")
    last_ts = int(klines[-1][0])
    if interval not in INTERVAL_MS:
        return _check("last_candle_closed", False, f"unknown interval: {interval}")
    close_time = last_ts + INTERVAL_MS[interval]
    ok = close_time <= now_ms
    return _check("last_candle_closed", ok, f"last_ts={last_ts} close_time={close_time} now={now_ms}")


def _check_last_processed_vs_latest(klines: list, state: dict, symbol: str) -> dict:
    if not klines:
        return _check("last_processed_vs_latest", False, "no klines")
    
    last_candle_ts = int(klines[-1][0])
    
    # ✅ استخراج از دیکشنری (پشتیبانی از ساختار جدید)
    lpct_dict = state.get("last_processed_candle_ts", {})
    if not isinstance(lpct_dict, dict):
        lpct_dict = {"BTC-SWAP-USDT": lpct_dict} # سازگاری با state های بسیار قدیمی

    lpct = lpct_dict.get(symbol)

    if lpct is None:
        return _check(
            "last_processed_vs_latest", False, 
            f"last_processed for {symbol} is None (never processed)", 
            level="warning"
        )

    if lpct > last_candle_ts:
        return _check("last_processed_vs_latest", False, f"last_processed={lpct} > latest_candle={last_candle_ts}")

    return _check("last_processed_vs_latest", True, f"last_processed={lpct} latest_candle={last_candle_ts}")


def _check_missing_candles(klines: list, state: dict, interval: str, symbol: str) -> dict:
    if not klines or interval not in INTERVAL_MS:
        return _check("no_missing_candles", False, "insufficient data to compute gap", level="warning")

    lpct_dict = state.get("last_processed_candle_ts", {})
    if not isinstance(lpct_dict, dict):
        lpct_dict = {"BTC-SWAP-USDT": lpct_dict}

    lpct = lpct_dict.get(symbol)

    if lpct is None:
        return _check("no_missing_candles", False, f"last_processed for {symbol} is None", level="warning")

    interval_ms = INTERVAL_MS[interval]
    last_candle_ts = int(klines[-1][0])

    if last_candle_ts <= lpct:
        return _check("no_missing_candles", True, f"no gap (last={last_candle_ts}, lpct={lpct})")

    delta = last_candle_ts - lpct
    missing = (delta // interval_ms) - 1
    if missing < 0: missing = 0

    if missing >= GAP_WARNING_THRESHOLD:
        return _check("no_missing_candles", False, f"{missing} candle(s) missing for {symbol} between lpct={lpct} and last={last_candle_ts}", level="warning")

    return _check("no_missing_candles", True, f"no significant gap (missing={missing})")


def _check_hash_match(state: dict) -> dict:
    expected = compute_strategy_hash()
    actual = state.get("strategy_hash")
    if actual is None:
        return _check("hash_match", False, "strategy_hash missing in state")
    return _check("hash_match", actual == expected, f"state={actual[:16]}... expected={expected[:16]}...")


# ============================================================
# Pure API
# ============================================================

def reconcile_with_data(state: dict, klines: list, interval: str, now_ms: int, symbol: str = "BTC-SWAP-USDT") -> dict:
    checks = []
    checks.append(_check_hash_match(state))
    checks.append(_check_klines(klines))
    checks.append(_check_last_candle_closed(klines, interval, now_ms))
    checks.append(_check_last_processed_vs_latest(klines, state, symbol))
    checks.append(_check_missing_candles(klines, state, interval, symbol))

    status = _aggregate_status(checks)
    return _result(status, checks, _summarize(checks))


# ============================================================
# Public API
# ============================================================

def reconcile_public(state_dir: str, symbol: str = "BTC-SWAP-USDT", interval: str = "1h") -> dict:
    checks = []

    chk_exists = _check_state_exists(state_dir)
    checks.append(chk_exists)
    if not chk_exists["ok"]:
        return _result(STATUS_ERROR, checks, "state.json missing → ERROR")

    chk_load, state = _check_state_loads(state_dir)
    checks.append(chk_load)
    if not chk_load["ok"]:
        return _result(STATUS_ERROR, checks, "state.json invalid → ERROR")

    checks.append(_check_hash_match(state))

    try:
        klines = fetch_klines(symbol, interval, limit=300)
    except Exception as e:
        checks.append(_check("klines_fetched", False, f"data_feed failed: {e}"))
        return _result(STATUS_ERROR, checks, "data_feed failed → ERROR")

    checks.append(_check("klines_fetched", True, f"received {len(klines)} klines"))
    checks.append(_check_klines(klines))

    if not klines:
        return _result(_aggregate_status(checks), checks, "no klines → cannot proceed")

    now_ms = int(time.time() * 1000)
    inner = reconcile_with_data(state, klines, interval, now_ms, symbol=symbol)
    for c in inner["checks"]:
        if c["name"] not in ("hash_match", "klines_sufficient"):
            checks.append(c)

    status = _aggregate_status(checks)
    return _result(status, checks, _summarize(checks))


if __name__ == "__main__":
    import sys
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")

    result = reconcile_public(state_dir, symbol, interval)
    print(f"STATUS: {result['status']} | {result['summary']}")
    for c in result["checks"]:
        mark = "✅" if c["ok"] else ("❌" if c["level"] == "error" else "⚠️ ")
        print(f"  {mark} [{c['name']}] {c['detail']}")
    if result["status"] == "ERROR": sys.exit(1)