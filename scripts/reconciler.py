"""
reconciler.py — Public Dry-Run Reconciliation.

هدف: بررسی سلامت داده‌ی عمومی و سازگاری state ذخیره‌شده.

هیچ تغییری در state.json یا events.jsonl ایجاد نمی‌شود.
هیچ سفارشی ارسال نمی‌شود.
هیچ API خصوصی صدا زده نمی‌شود.

قراردادها:
  - status ∈ {"OK", "WARNING", "ERROR"}
  - خروجی dict، deterministic
  - اگر last_processed_candle_ts غایب/None باشد، صریح گزارش می‌شود

تفکیک توابع:
  - reconcile_with_data(state, klines, interval, now_ms) → dict
      pure: no HTTP, no disk I/O. 5 checks:
        hash_match, klines_sufficient, last_candle_closed,
        last_processed_vs_latest, no_missing_candles
  - reconcile_public(state_dir, symbol, interval) → dict
      wrapper: state read + klines fetch + merge با pure checks

سیاست strict در Reconciler:
  - Reconciler باید state با hash متفاوت را هم بخواند تا بتواند
    mismatch را تشخیص بدهد.
  - سیاست fail-closed (strict=True) در run_paper اعمال می‌شود.

سیاست زمان:
  - now_ms در reconcile_with_data اجباری است (deterministic).
  - در reconcile_public از time.time() استفاده می‌شود، نه فرمول
    مشتق‌شده از کندل آخر (که می‌تواند کندل باز را بسته تلقی کند).

Spec: docs/step9a_paper_persistence.md
"""

import os
import time

from data_feed import fetch_klines, INTERVAL_MS
from paper_state_manager import load_state, state_exists
from compute_hash import compute_strategy_hash


# ============================================================
# Constants
# ============================================================

MIN_KLINES = 250
GAP_WARNING_THRESHOLD = 2

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_ERROR = "ERROR"


# ============================================================
# Helpers
# ============================================================

def _result(status: str, checks: list, summary: str) -> dict:
    return {
        "status": status,
        "checks": checks,
        "summary": summary,
    }


def _check(name: str, ok: bool, detail: str,
           level: str = "error") -> dict:
    return {
        "name": name,
        "ok": ok,
        "level": level,
        "detail": detail,
    }


def _aggregate_status(checks: list) -> str:
    has_error = any(not c["ok"] and c["level"] == "error" for c in checks)
    has_warning = any(not c["ok"] and c["level"] == "warning" for c in checks)
    if has_error:
        return STATUS_ERROR
    if has_warning:
        return STATUS_WARNING
    return STATUS_OK


def _summarize(checks: list) -> str:
    n_error = sum(1 for c in checks
                  if not c["ok"] and c["level"] == "error")
    n_warn = sum(1 for c in checks
                 if not c["ok"] and c["level"] == "warning")
    return f"{len(checks)} checks: {n_error} error, {n_warn} warning"


# ============================================================
# Individual checks (pure)
# ============================================================

def _check_state_exists(state_dir: str) -> dict:
    exists = state_exists(state_dir)
    return _check(
        "state_exists", exists,
        f"state.json in {state_dir}",
    )


def _check_state_loads(state_dir: str) -> tuple:
    """
    بارگذاری state با strict=False.

    Reconciler وظیفه‌اش تشخیص است، نه fail-closed.
    """
    try:
        state = load_state(state_dir, strict=False)
        return _check("state_loads", True, "state.json loaded"), state
    except Exception as e:
        return _check("state_loads", False, f"load failed: {e}"), None


def _check_klines(klines: list) -> dict:
    n = len(klines)
    ok = n >= MIN_KLINES
    return _check(
        "klines_sufficient", ok,
        f"received {n} klines (min={MIN_KLINES})",
        level="warning",
    )


def _check_last_candle_closed(klines: list, interval: str,
                               now_ms: int) -> dict:
    if not klines:
        return _check(
            "last_candle_closed", False,
            "no klines to check",
        )
    last_ts = int(klines[-1][0])
    if interval not in INTERVAL_MS:
        return _check(
            "last_candle_closed", False,
            f"unknown interval: {interval}",
        )
    close_time = last_ts + INTERVAL_MS[interval]
    ok = close_time <= now_ms
    return _check(
        "last_candle_closed", ok,
        f"last_ts={last_ts} close_time={close_time} now={now_ms}",
    )


def _check_last_processed_vs_latest(klines: list,
                                     state: dict) -> dict:
    if not klines:
        return _check(
            "last_processed_vs_latest", False,
            "no klines",
        )
    last_candle_ts = int(klines[-1][0])

    if "last_processed_candle_ts" not in state:
        return _check(
            "last_processed_vs_latest", False,
            "last_processed_candle_ts missing in state",
            level="warning",
        )

    lpct = state["last_processed_candle_ts"]

    if lpct is None:
        return _check(
            "last_processed_vs_latest", False,
            "last_processed_candle_ts is None "
            "(state never processed a candle)",
            level="warning",
        )

    if lpct > last_candle_ts:
        return _check(
            "last_processed_vs_latest", False,
            f"last_processed={lpct} > latest_candle={last_candle_ts}",
        )

    return _check(
        "last_processed_vs_latest", True,
        f"last_processed={lpct} latest_candle={last_candle_ts}",
    )


def _check_missing_candles(klines: list, state: dict,
                            interval: str) -> dict:
    if not klines or interval not in INTERVAL_MS:
        return _check(
            "no_missing_candles", False,
            "insufficient data to compute gap",
            level="warning",
        )

    lpct = state.get("last_processed_candle_ts")
    if lpct is None:
        return _check(
            "no_missing_candles", False,
            "last_processed_candle_ts is None",
            level="warning",
        )

    interval_ms = INTERVAL_MS[interval]
    last_candle_ts = int(klines[-1][0])

    if last_candle_ts <= lpct:
        return _check(
            "no_missing_candles", True,
            f"no gap (last={last_candle_ts}, lpct={lpct})",
        )

    delta = last_candle_ts - lpct
    missing = (delta // interval_ms) - 1
    if missing < 0:
        missing = 0

    if missing >= GAP_WARNING_THRESHOLD:
        return _check(
            "no_missing_candles", False,
            f"{missing} candle(s) missing between "
            f"lpct={lpct} and last={last_candle_ts}",
            level="warning",
        )

    return _check(
        "no_missing_candles", True,
        f"no significant gap (missing={missing})",
    )


def _check_hash_match(state: dict) -> dict:
    expected = compute_strategy_hash()
    actual = state.get("strategy_hash")
    if actual is None:
        return _check(
            "hash_match", False,
            "strategy_hash missing in state",
        )
    ok = (actual == expected)
    return _check(
        "hash_match", ok,
        f"state={actual[:16]}... expected={expected[:16]}...",
    )


# ============================================================
# Pure API — reconcile_with_data
# ============================================================

def reconcile_with_data(state: dict, klines: list,
                        interval: str, now_ms: int) -> dict:
    """
    Reconciliation با داده‌ی از قبل دریافت‌شده.

    Pure function: بدون HTTP، بدون disk I/O.
    مقادیر state و klines را دست‌نخورده باقی می‌گذارد.

    Args:
        state     : dict (هر hash — بررسی hash_match در همین تابع)
        klines    : list of candles (اعتبارسنجی‌شده توسط data_feed)
        interval  : e.g. "1h"
        now_ms    : timestamp فعلی (اجباری؛ برای deterministic بودن)

    Returns:
        {"status": "OK"|"WARNING"|"ERROR",
         "checks": [{"name","ok","level","detail"}, ...],
         "summary": str}

    Note:
        این تابع تصمیم «ادامه یا توقف» نمی‌گیرد. run_paper.main
        بر اساس status تصمیم می‌گیرد.
    """
    checks = []
    checks.append(_check_hash_match(state))
    checks.append(_check_klines(klines))
    checks.append(_check_last_candle_closed(klines, interval, now_ms))
    checks.append(_check_last_processed_vs_latest(klines, state))
    checks.append(_check_missing_candles(klines, state, interval))

    status = _aggregate_status(checks)
    return _result(status, checks, _summarize(checks))


# ============================================================
# Public API — reconcile_public
# ============================================================

def reconcile_public(state_dir: str, symbol: str = "BTC-SWAP-USDT",
                     interval: str = "1h") -> dict:
    """
    بررسی کامل: state + klines + checks.

    این تابع فقط می‌خواند. هیچ تغییری ایجاد نمی‌کند.

    ترتیب checkهای خروجی (حفظ‌شده از نسخه‌ی قبلی):
      1. state_exists
      2. state_loads
      3. hash_match
      4. klines_fetched
      5. klines_sufficient
      6. last_candle_closed
      7. last_processed_vs_latest
      8. no_missing_candles

    Returns:
        {"status", "checks", "summary"}
    """
    checks = []

    # ---- 1. State existence ----
    chk_exists = _check_state_exists(state_dir)
    checks.append(chk_exists)
    if not chk_exists["ok"]:
        return _result(STATUS_ERROR, checks,
                       "state.json missing → ERROR")

    # ---- 2. State loads ----
    chk_load, state = _check_state_loads(state_dir)
    checks.append(chk_load)
    if not chk_load["ok"]:
        return _result(STATUS_ERROR, checks,
                       "state.json invalid → ERROR")

    # ---- 3. Hash match ----
    checks.append(_check_hash_match(state))

    # ---- 4. Fetch klines ----
    try:
        klines = fetch_klines(symbol, interval, limit=300)
    except Exception as e:
        checks.append(_check(
            "klines_fetched", False,
            f"data_feed failed: {e}",
        ))
        return _result(STATUS_ERROR, checks,
                       "data_feed failed → ERROR")

    checks.append(_check("klines_fetched", True,
                         f"received {len(klines)} klines"))

    # ---- 5. Klines sufficient (single check) ----
    checks.append(_check_klines(klines))

    if not klines:
        return _result(_aggregate_status(checks), checks,
                       "no klines → cannot proceed")

    # ---- 6-8. Pure checks (skip hash_match and klines_sufficient) ----
    now_ms = int(time.time() * 1000)
    inner = reconcile_with_data(state, klines, interval, now_ms)
    for c in inner["checks"]:
        if c["name"] in ("hash_match", "klines_sufficient"):
            continue  # قبلاً اضافه شده‌اند
        checks.append(c)

    # ---- Aggregate ----
    status = _aggregate_status(checks)
    return _result(status, checks, _summarize(checks))


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")

    result = reconcile_public(state_dir, symbol, interval)

    print("=" * 70)
    print("🔍 PUBLIC RECONCILE — DRY RUN")
    print("=" * 70)
    print(f"  state_dir : {state_dir}")
    print(f"  symbol    : {symbol}")
    print(f"  interval  : {interval}")
    print("=" * 70)
    print()

    for c in result["checks"]:
        mark = "✅" if c["ok"] else (
            "❌" if c["level"] == "error" else "⚠️ "
        )
        print(f"  {mark} [{c['name']}] {c['detail']}")
    print()
    print("=" * 70)
    print(f"  STATUS : {result['status']}")
    print(f"  {result['summary']}")
    print("=" * 70)

    if result["status"] == "ERROR":
        sys.exit(1)