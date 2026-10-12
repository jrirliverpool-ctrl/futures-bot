"""
probe_binance_history.py — تأیید دریافت داده‌ی تاریخی چندساله از Binance.

v2.2 (نسبت به v2.1):
  - افزودن --known-gaps PATH برای بارگذاری فهرست مرجع شکاف‌ها.
  - بررسی دقیق اینکه timestampهای مفقود در فهرست known هستند یا خیر.
  - سه مقدار جدید completeness: complete / complete_with_known_gaps / incomplete
  - فیلدهای جدید: missing_known, missing_unknown, unknown_missing_sample
  - تحلیل timestamp اکنون missing_ts_list را برمی‌گرداند.

اصول:
  - هیچ کندل مصنوعی ساخته نمی‌شود.
  - complete_with_known_gaps معادل complete نیست و در گزارش صریح است.
  - شکاف ناشناخته → incomplete و exit code 1.
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from binance_feed import (
    INTERVAL_MS,
    SUPPORTED_INTERVALS,
    fetch_binance_klines_range,
    filter_closed_binance_candles,
    summarize_candles,
    InvalidCandleError,
    TruncatedDataError,
)


MODE_PROFILES = {
    "short": {
        "fixed_start": None,
        "start_offset_days": 180,
        "max_candles": 10_000,
        "max_iterations": 20,
        "max_seconds": 120,
    },
    "long": {
        "fixed_start": "2019-01-01T00:00:00Z",
        "start_offset_days": None,
        "max_candles": 120_000,
        "max_iterations": 300,
        "max_seconds": 1800,
    },
}

DEFAULT_MIN_REQUEST_WINDOW_S = 0.5
RETRY_COUNT_METHOD = "estimated_from_session_trace"
KNOWN_GAPS_SCHEMA_VERSION_SUPPORTED = ("1.0.0",)


# ============================================================
# Exceptions
# ============================================================

class ProbeAborted(Exception):
    pass


class DeadlineExceeded(ProbeAborted):
    pass


class RateLimitBan(ProbeAborted):
    pass


class KnownGapsError(Exception):
    """خطا در بارگذاری یا اعتبارسنجی فایل known_gaps."""


# ============================================================
# Instrumented session
# ============================================================

class InstrumentedSession:
    def __init__(self, inner=None, deadline_monotonic=None,
                 min_request_window_s=DEFAULT_MIN_REQUEST_WINDOW_S):
        self.inner = inner if inner is not None else requests
        self.deadline_monotonic = deadline_monotonic
        self.min_request_window_s = min_request_window_s
        self.records = []
        self.aborted = False
        self.abort_reason = None

    def _effective_timeout(self, requested):
        if self.deadline_monotonic is None:
            return requested
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining < self.min_request_window_s:
            self.aborted = True
            self.abort_reason = self.abort_reason or "max_seconds exceeded"
            raise DeadlineExceeded(
                f"remaining {remaining:.3f}s < "
                f"min_request_window_s={self.min_request_window_s}"
            )
        base = float(requested) if requested is not None else 15.0
        return max(self.min_request_window_s, min(base, remaining))

    def get(self, url, params=None, timeout=None):
        if self.aborted:
            raise RateLimitBan(self.abort_reason or "session aborted")

        effective_timeout = self._effective_timeout(timeout)
        t0 = time.monotonic()
        try:
            response = self.inner.get(
                url, params=params, timeout=effective_timeout,
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            self.records.append({
                "url": url,
                "params": dict(params) if params else {},
                "outcome": type(exc).__name__,
                "status": None,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "retry_after": None,
            })
            raise

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        status = response.status_code
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After")

        self.records.append({
            "url": url,
            "params": dict(params) if params else {},
            "outcome": "ok",
            "status": status,
            "elapsed_ms": elapsed_ms,
            "retry_after": retry_after,
        })

        if status == 418:
            self.aborted = True
            self.abort_reason = "418 received"
            raise RateLimitBan("418 received")

        return response


# ============================================================
# Time helpers
# ============================================================

def _parse_iso_utc(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _floor_to_interval(ms, interval_ms):
    return (ms // interval_ms) * interval_ms


def _ceil_to_interval(ms, interval_ms):
    if ms % interval_ms == 0:
        return ms
    return ((ms // interval_ms) + 1) * interval_ms


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def resolve_window(start_iso, end_iso, interval, now_ms=None, align="ceil"):
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if align not in ("floor", "ceil"):
        raise ValueError(f"Unsupported align: {align}")

    interval_ms = INTERVAL_MS[interval]
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    requested_start_ms = _parse_iso_utc(start_iso)
    requested_end = _parse_iso_utc(end_iso) if end_iso else now_ms
    requested_end = min(requested_end, now_ms)

    if align == "floor":
        eff_start_ms = _floor_to_interval(requested_start_ms, interval_ms)
    else:
        eff_start_ms = _ceil_to_interval(requested_start_ms, interval_ms)

    candidate_end = _floor_to_interval(
        requested_end - interval_ms, interval_ms,
    )
    latest_closed = _floor_to_interval(now_ms - interval_ms, interval_ms)
    eff_end_ms = min(candidate_end, latest_closed)

    if eff_end_ms < eff_start_ms:
        raise ValueError(
            f"effective end ({_iso(eff_end_ms)}) < start "
            f"({_iso(eff_start_ms)})"
        )

    expected_count = (eff_end_ms - eff_start_ms) // interval_ms + 1
    binance_end_ms = eff_end_ms + interval_ms

    return {
        "eff_start_ms": eff_start_ms,
        "eff_end_ms": eff_end_ms,
        "binance_end_ms": binance_end_ms,
        "expected_count": expected_count,
        "requested_start_ms": requested_start_ms,
        "align": align,
    }


# ============================================================
# Known gaps loader
# ============================================================

def load_known_gaps(path, symbol, interval):
    """
    بارگذاری و اعتبارسنجی فایل known_gaps.

    Raises:
        KnownGapsError: اگر فایل نباشد، schema ناسازگار باشد، یا
        symbol/interval با فراخوانی جاری مطابقت نداشته باشد،
        یا محاسبات count با start_iso/end_iso سازگار نباشد.
    """
    p = Path(path)
    if not p.exists():
        raise KnownGapsError(f"known_gaps file not found: {path}")

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise KnownGapsError(f"cannot parse known_gaps JSON: {e}")

    schema = data.get("schema_version")
    if schema not in KNOWN_GAPS_SCHEMA_VERSION_SUPPORTED:
        raise KnownGapsError(
            f"unsupported schema_version: {schema!r}; "
            f"supported: {KNOWN_GAPS_SCHEMA_VERSION_SUPPORTED}"
        )

    if data.get("symbol") != symbol:
        raise KnownGapsError(
            f"known_gaps symbol {data.get('symbol')!r} does not match "
            f"requested symbol {symbol!r}"
        )
    if data.get("interval") != interval:
        raise KnownGapsError(
            f"known_gaps interval {data.get('interval')!r} does not match "
            f"requested interval {interval!r}"
        )

    gaps = data.get("gaps")
    if not isinstance(gaps, list):
        raise KnownGapsError("known_gaps 'gaps' field must be a list")

    interval_ms = INTERVAL_MS[interval]
    validated = []

    for i, g in enumerate(gaps):
        try:
            start_ms = _parse_iso_utc(g["start_iso"])
            end_ms = _parse_iso_utc(g["end_iso"])
        except Exception as e:
            raise KnownGapsError(f"gap #{i}: invalid iso timestamps: {e}")

        if end_ms < start_ms:
            raise KnownGapsError(
                f"gap #{i}: end_iso < start_iso"
            )
        if (end_ms - start_ms) % interval_ms != 0:
            raise KnownGapsError(
                f"gap #{i}: start_iso/end_iso not aligned to interval "
                f"{interval}"
            )

        computed_count = (end_ms - start_ms) // interval_ms + 1
        declared_count = int(g.get("count", -1))
        if computed_count != declared_count:
            raise KnownGapsError(
                f"gap #{i}: count mismatch — declared {declared_count}, "
                f"computed {computed_count}"
            )

        validated.append({
            "id": g.get("id", i + 1),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "count": declared_count,
            "cause": g.get("cause"),
            "cause_confidence": g.get("cause_confidence"),
            "candidate_cause": g.get("candidate_cause"),
            "start_iso": g["start_iso"],
            "end_iso": g["end_iso"],
        })

    return {
        "schema_version": schema,
        "symbol": data["symbol"],
        "interval": data["interval"],
        "source": data.get("source"),
        "gaps": validated,
        "summary": data.get("summary") or {},
    }


def build_known_ts_set(known_gaps_doc, interval):
    """ساخت مجموعه‌ی timestampهای known (میلی‌ثانیه)."""
    interval_ms = INTERVAL_MS[interval]
    ts_set = set()
    for g in known_gaps_doc["gaps"]:
        ts = g["start_ms"]
        while ts <= g["end_ms"]:
            ts_set.add(ts)
            ts += interval_ms
    return ts_set


# ============================================================
# Timestamp set analysis
# ============================================================

def analyze_timestamps(closed_candles, eff_start_ms, eff_end_ms, interval):
    interval_ms = INTERVAL_MS[interval]
    expected_count = (eff_end_ms - eff_start_ms) // interval_ms + 1

    actual_ts = set()
    duplicates = 0
    extra_count = 0
    for c in closed_candles:
        ts = int(c[0])
        if ts < eff_start_ms or ts > eff_end_ms:
            extra_count += 1
            continue
        if (ts - eff_start_ms) % interval_ms != 0:
            extra_count += 1
            continue
        if ts in actual_ts:
            duplicates += 1
        else:
            actual_ts.add(ts)

    missing_ts_list = []
    missing_ranges = []
    current_run_start = None

    for i in range(expected_count):
        ts = eff_start_ms + i * interval_ms
        if ts not in actual_ts:
            if current_run_start is None:
                current_run_start = ts
            missing_ts_list.append(ts)
        else:
            if current_run_start is not None:
                missing_ranges.append({
                    "start_ms": current_run_start,
                    "end_ms": ts - interval_ms,
                    "count": (ts - current_run_start) // interval_ms,
                })
                current_run_start = None

    if current_run_start is not None:
        missing_ranges.append({
            "start_ms": current_run_start,
            "end_ms": eff_end_ms,
            "count": (eff_end_ms - current_run_start) // interval_ms + 1,
        })

    missing_count = len(missing_ts_list)
    missing_pct = (
        (missing_count / expected_count * 100) if expected_count else 0.0
    )

    missing_sample = []
    for r in missing_ranges[:10]:
        missing_sample.append({
            "start_iso": _iso(r["start_ms"]),
            "end_iso": _iso(r["end_ms"]),
            "count": r["count"],
        })

    return {
        "duplicates": duplicates,
        "actual_unique": len(actual_ts),
        "extra_count": extra_count,
        "missing_count": missing_count,
        "missing_pct": round(missing_pct, 6),
        "missing_ranges_total": len(missing_ranges),
        "missing_ranges_sample": missing_sample,
        "missing_ts_list": missing_ts_list,
    }


# ============================================================
# Retry classification
# ============================================================

def _classify_record(rec):
    if rec["outcome"] != "ok":
        return "timeout"
    s = rec["status"]
    if s == 429:
        return "429"
    if s is not None and 500 <= s <= 599:
        return "5xx"
    return "other"


def _is_success(rec):
    return (
        rec["outcome"] == "ok"
        and rec["status"] is not None
        and 200 <= rec["status"] < 300
    )


def classify_attempts(records):
    empty = {
        "attempts_total": 0,
        "attempts_success": 0,
        "attempts_failed_429": 0,
        "attempts_failed_5xx": 0,
        "attempts_failed_timeout": 0,
        "attempts_failed_other": 0,
        "logical_calls": 0,
        "retries_total": 0,
        "retries_by_cause": {"429": 0, "5xx": 0, "timeout": 0, "other": 0},
        "method": RETRY_COUNT_METHOD,
    }
    if not records:
        return empty

    groups = []
    current = []
    for rec in records:
        if current:
            prev = current[-1]
            if _is_success(prev) or rec["params"] != prev["params"]:
                groups.append(current)
                current = []
        current.append(rec)
    if current:
        groups.append(current)

    attempts_success = 0
    failed = Counter()
    retries_by_cause = Counter()

    for g in groups:
        for r in g:
            if _is_success(r):
                attempts_success += 1
            else:
                failed[_classify_record(r)] += 1
        for r in g[:-1]:
            retries_by_cause[_classify_record(r)] += 1

    return {
        "attempts_total": len(records),
        "attempts_success": attempts_success,
        "attempts_failed_429": failed["429"],
        "attempts_failed_5xx": failed["5xx"],
        "attempts_failed_timeout": failed["timeout"],
        "attempts_failed_other": failed["other"],
        "logical_calls": len(groups),
        "retries_total": sum(retries_by_cause.values()),
        "retries_by_cause": {
            "429": retries_by_cause["429"],
            "5xx": retries_by_cause["5xx"],
            "timeout": retries_by_cause["timeout"],
            "other": retries_by_cause["other"],
        },
        "method": RETRY_COUNT_METHOD,
    }


# ============================================================
# CLI
# ============================================================

def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Binance history fetch probe (read-only)",
    )
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1h", choices=SUPPORTED_INTERVALS)
    p.add_argument("--mode", default="short", choices=("short", "long"))
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--align", default="ceil", choices=("floor", "ceil"))
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--max-seconds", type=int, default=None)
    p.add_argument("--min-request-window", type=float,
                   default=DEFAULT_MIN_REQUEST_WINDOW_S)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--backoff-base", type=float, default=0.5)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--sleep-between", type=float, default=0.15)
    p.add_argument("--known-gaps", default=None,
                   help="مسیر فایل known_gaps JSON (اختیاری)")
    p.add_argument("--out", default=None)

    args = p.parse_args(argv)
    profile = MODE_PROFILES[args.mode]

    if args.start is None:
        if profile["fixed_start"] is not None:
            args.start = profile["fixed_start"]
        else:
            offset_ms = profile["start_offset_days"] * 86_400_000
            args.start = _iso(int(time.time() * 1000) - offset_ms)
    if args.max_candles is None:
        args.max_candles = profile["max_candles"]
    if args.max_iterations is None:
        args.max_iterations = profile["max_iterations"]
    if args.max_seconds is None:
        args.max_seconds = profile["max_seconds"]

    return args


# ============================================================
# Reporting helpers
# ============================================================

def _new_report(args):
    return {
        "probe": "binance_history",
        "version": "v2.2",
        "started_at_iso": datetime.now(timezone.utc).isoformat(),
        "arguments": {
            "symbol": args.symbol,
            "interval": args.interval,
            "mode": args.mode,
            "start": args.start,
            "end": args.end,
            "align": args.align,
            "max_candles": args.max_candles,
            "max_iterations": args.max_iterations,
            "max_seconds": args.max_seconds,
            "min_request_window": args.min_request_window,
            "max_retries": args.max_retries,
            "backoff_base": args.backoff_base,
            "timeout": args.timeout,
            "known_gaps_file": args.known_gaps,
        },
        "window": None,
        "known_gaps": None,
        "fetch": None,
        "counts": None,
        "gaps_sample": None,
        "http": None,
        "open_candles_removed": None,
        "dropped_out_of_window": None,
        "fetch_status": "not_started",
        "completeness": "unknown",
        "truncation_reason": None,
        "verdict_ok": False,
        "has_known_gaps": False,
        "fetch_error": None,
        "report_write_error": None,
        "fatal_error": None,
    }


def _print_header(args):
    print("=" * 72)
    print("🔍 PROBE BINANCE HISTORY — Historical Fetch Verification (v2.2)")
    print("=" * 72)
    print(f"  symbol     : {args.symbol}")
    print(f"  interval   : {args.interval}")
    print(f"  mode       : {args.mode}")
    print(f"  start      : {args.start}  (align={args.align})")
    print(f"  end        : {args.end or 'now'}")
    print(f"  max_candles: {args.max_candles}")
    print(f"  max_iter   : {args.max_iterations}")
    print(f"  max_seconds: {args.max_seconds}")
    print(f"  min_req_win: {args.min_request_window}s")
    print(f"  known_gaps : {args.known_gaps or '(none)'}")
    print(f"  purpose    : تأیید پوشش تاریخی + صفحه‌بندی (نه Backtest)")
    print("=" * 72)


def _finalize_and_print(report, out_path):
    print()
    print("=" * 72)
    if report["verdict_ok"]:
        print("✅ BINANCE HISTORY FETCH VERIFIED")
    else:
        print("⚠️ BINANCE HISTORY FETCH — ISSUES DETECTED")
    print("=" * 72)

    w = report.get("window") or {}
    c = report.get("counts") or {}
    h = report.get("http") or {}
    f = report.get("fetch") or {}
    kg = report.get("known_gaps")

    print(f"  mode             : {report['arguments']['mode']}")
    print(f"  symbol/interval  : "
          f"{report['arguments']['symbol']} / "
          f"{report['arguments']['interval']}")
    print(f"  requested_start  : {w.get('requested_start_iso')}")
    print(f"  effective_start  : {w.get('effective_start_iso')} "
          f"(align={w.get('align')})")
    print(f"  effective_end    : {w.get('effective_end_iso')}")
    print(f"  expected/actual  : "
          f"{w.get('expected_count')}/{c.get('actual')}")
    print(f"  missing          : {c.get('missing')} "
          f"({c.get('missing_pct', 0):.4f}%)")
    if kg:
        print(f"  known_gaps_used  : {kg.get('gaps_total')} gaps, "
              f"{kg.get('candles_total')} candles in reference")
        print(f"  missing_known    : {c.get('missing_known')}")
        print(f"  missing_unknown  : {c.get('missing_unknown')}")
    else:
        print(f"  known_gaps_used  : (none — همه‌ی missing ناشناخته)")
    print(f"  duplicates       : {c.get('duplicates')}")
    print(f"  extra            : {c.get('extra')}")
    print(f"  gaps             : {c.get('gaps_total')} "
          f"(missing {c.get('total_missing_from_gaps')})")
    print(f"  open_removed     : {report.get('open_candles_removed')}")
    print(f"  dropped_oow      : {report.get('dropped_out_of_window')}")
    print(f"  fetch_status     : {report['fetch_status']}")
    print(f"  completeness     : {report.get('completeness', 'unknown')}")
    print(f"  has_known_gaps   : {report.get('has_known_gaps')}")
    print(f"  truncation       : {report.get('truncation_reason')}")
    print(f"  requests         : {h.get('attempts_total')} "
          f"(retries: {h.get('retries_total')}, "
          f"method={h.get('method')})")
    print(f"  retries_by_cause : {h.get('retries_by_cause')}")
    print(f"  418 seen         : "
          f"{h.get('abort_reason') == '418 received'}")
    print(f"  wall_clock_s     : {f.get('wall_clock_s')}")
    if report.get("fetch_error"):
        print(f"  fetch_error      : {report['fetch_error']}")
    if report.get("report_write_error"):
        print(f"  report_write_err : {report['report_write_error']}")
    if report.get("fatal_error"):
        print(f"  fatal_error      : {report['fatal_error']}")

    print()
    print("  ⚠️ این Probe فقط پوشش و ساختار داده را تأیید می‌کند.")
    print("  ⚠️ اثبات سودآوری استراتژی نیست و Backtest اجرا نمی‌کند.")
    print("  ⚠️ complete_with_known_gaps معادل complete نیست.")
    print("=" * 72)

    write_ok = True
    if out_path:
        try:
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"\n  📝 report written to {out_path}")
        except Exception as e:
            report["report_write_error"] = f"{type(e).__name__}: {e}"
            print(f"\n  ❌ failed to write report: "
                  f"{type(e).__name__}: {e}")
            write_ok = False
    return write_ok


# ============================================================
# Main
# ============================================================

def main(argv=None):
    args = _parse_args(argv)
    report = _new_report(args)
    _print_header(args)

    session = None
    exit_code = 1

    try:
        # ---- Stage 0: load known_gaps (if provided) ----
        known_ts_set = set()
        known_gaps_doc = None

        if args.known_gaps:
            print("\n[Stage 0] Loading known_gaps reference...")
            try:
                known_gaps_doc = load_known_gaps(
                    args.known_gaps, args.symbol, args.interval,
                )
                known_ts_set = build_known_ts_set(
                    known_gaps_doc, args.interval,
                )
                report["known_gaps"] = {
                    "file": args.known_gaps,
                    "schema_version": known_gaps_doc["schema_version"],
                    "symbol": known_gaps_doc["symbol"],
                    "interval": known_gaps_doc["interval"],
                    "gaps_total": len(known_gaps_doc["gaps"]),
                    "candles_total": len(known_ts_set),
                }
                print(f"  file        : {args.known_gaps}")
                print(f"  schema      : {known_gaps_doc['schema_version']}")
                print(f"  gaps loaded : {len(known_gaps_doc['gaps'])}")
                print(f"  candles set : {len(known_ts_set)}")
                print("  ✅ known_gaps loaded")
            except KnownGapsError as e:
                print(f"  ❌ known_gaps error: {e}")
                report["fetch_status"] = "known_gaps_error"
                report["fetch_error"] = f"KnownGapsError: {e}"
                report["verdict_ok"] = False
                return 1
        else:
            print("\n[Stage 0] known_gaps: not provided")

        # ---- Stage 1: window ----
        print("\n[Stage 1] Resolving effective window (closed candles only)...")
        win = resolve_window(
            args.start, args.end, args.interval, align=args.align,
        )
        report["window"] = {
            "requested_start_iso": _iso(win["requested_start_ms"]),
            "effective_start_iso": _iso(win["eff_start_ms"]),
            "effective_end_iso": _iso(win["eff_end_ms"]),
            "effective_start_ms": win["eff_start_ms"],
            "effective_end_ms": win["eff_end_ms"],
            "expected_count": win["expected_count"],
            "align": win["align"],
        }
        print(f"  requested_start: {_iso(win['requested_start_ms'])}")
        print(f"  eff_start_iso  : {_iso(win['eff_start_ms'])}")
        print(f"  eff_end_iso    : {_iso(win['eff_end_ms'])}")
        print(f"  expected_count : {win['expected_count']}")
        print("  ✅ window resolved")

        # ---- Stage 2: fetch ----
        print("\n[Stage 2] Fetching range (strict_completeness=False)...")
        deadline = time.monotonic() + args.max_seconds
        session = InstrumentedSession(
            deadline_monotonic=deadline,
            min_request_window_s=args.min_request_window,
        )

        t0 = time.monotonic()
        fetch_error = None
        aborted_reason = None
        raw = []

        try:
            raw = fetch_binance_klines_range(
                args.symbol, args.interval,
                start_ms=win["eff_start_ms"],
                end_ms=win["binance_end_ms"],
                max_candles=args.max_candles,
                max_iterations=args.max_iterations,
                max_retries=args.max_retries,
                backoff_base=args.backoff_base,
                timeout=args.timeout,
                session=session,
                sleep_between_requests=args.sleep_between,
                strict_completeness=False,
            )
            report["fetch_status"] = "ok"
        except RateLimitBan as e:
            aborted_reason = "418"
            fetch_error = f"RateLimitBan: {e}"
            report["fetch_status"] = "aborted_418"
        except DeadlineExceeded as e:
            aborted_reason = "deadline"
            fetch_error = f"DeadlineExceeded: {e}"
            report["fetch_status"] = "aborted_deadline"
        except requests.HTTPError as e:
            fetch_error = f"HTTPError: {e}"
            if session.aborted and session.abort_reason == "418 received":
                aborted_reason = "418"
                report["fetch_status"] = "aborted_418"
            else:
                report["fetch_status"] = "http_error"
        except TruncatedDataError as e:
            fetch_error = f"TruncatedDataError: {e}"
            report["fetch_status"] = "truncated_module_error"
        except InvalidCandleError as e:
            fetch_error = f"InvalidCandleError: {e}"
            report["fetch_status"] = "invalid_candle"
        except Exception as e:
            fetch_error = f"{type(e).__name__}: {e}"
            report["fetch_status"] = "error"

        wall_s = time.monotonic() - t0
        report["fetch"] = {
            "raw_returned": len(raw),
            "wall_clock_s": round(wall_s, 3),
            "abort_reason": aborted_reason,
        }
        report["fetch_error"] = fetch_error

        stats = classify_attempts(session.records)
        stats["session_aborted"] = session.aborted
        stats["abort_reason"] = session.abort_reason
        report["http"] = stats

        print(f"  raw candles returned : {len(raw)}")
        print(f"  wall_clock_s         : {wall_s:.2f}")
        if fetch_error:
            print(f"  fetch_error          : {fetch_error}")
        if aborted_reason:
            print(f"  aborted_reason       : {aborted_reason}")

        if report["fetch_status"] != "ok":
            report["completeness"] = "unknown"
            report["truncation_reason"] = None
            report["verdict_ok"] = False
            exit_code = 1
        else:
            # ---- Stage 3: filter ----
            print("\n[Stage 3] Filtering (closed candles, in-window)...")
            now_ms = int(time.time() * 1000)

            window_only = [
                c for c in raw
                if win["eff_start_ms"] <= int(c[0]) <= win["eff_end_ms"]
            ]
            dropped_oow = len(raw) - len(window_only)

            closed = filter_closed_binance_candles(
                window_only, args.interval, now_ms=now_ms,
            )
            open_removed = len(window_only) - len(closed)

            report["open_candles_removed"] = open_removed
            report["dropped_out_of_window"] = dropped_oow

            print(f"  dropped (out of window) : {dropped_oow}")
            print(f"  dropped (open candle)   : {open_removed}")
            print(f"  final count             : {len(closed)}")

            print("\n  Structural diagnostics (summarize_candles):")
            s = summarize_candles(closed, args.interval)
            print(f"    count       : {s['count']}")
            print(f"    first_iso   : {s['first_iso']}")
            print(f"    last_iso    : {s['last_iso']}")
            print(f"    duplicates  : {s['duplicates']}")
            print(f"    gap_count   : {s['gap_count']}")
            print(f"    total_miss  : {s['total_missing']}")

            print("\n  Timestamp set analysis (authoritative):")
            tsa = analyze_timestamps(
                closed,
                win["eff_start_ms"], win["eff_end_ms"], args.interval,
            )
            expected = win["expected_count"]
            actual_unique = tsa["actual_unique"]
            missing = tsa["missing_count"]

            missing_ts = set(tsa["missing_ts_list"])
            known_missing_ts = missing_ts & known_ts_set
            unknown_missing_ts = missing_ts - known_ts_set

            print(f"    expected        : {expected}")
            print(f"    actual_unique   : {actual_unique}")
            print(f"    missing         : {missing} "
                  f"({tsa['missing_pct']:.4f}%)")
            print(f"    missing_known   : {len(known_missing_ts)}")
            print(f"    missing_unknown : {len(unknown_missing_ts)}")
            print(f"    missing_ranges  : {tsa['missing_ranges_total']}")
            print(f"    duplicates      : {tsa['duplicates']}")
            print(f"    extra           : {tsa['extra_count']}")

            unknown_sample = [
                _iso(ts) for ts in sorted(unknown_missing_ts)[:20]
            ]

            report["counts"] = {
                "expected": expected,
                "actual": len(closed),
                "actual_unique": actual_unique,
                "missing": missing,
                "missing_pct": tsa["missing_pct"],
                "missing_known": len(known_missing_ts),
                "missing_unknown": len(unknown_missing_ts),
                "missing_ranges_total": tsa["missing_ranges_total"],
                "missing_ranges_sample": tsa["missing_ranges_sample"],
                "unknown_missing_sample": unknown_sample,
                "duplicates": tsa["duplicates"],
                "extra": tsa["extra_count"],
                "first_iso": s["first_iso"],
                "last_iso": s["last_iso"],
                "gaps_total": s["gap_count"],
                "total_missing_from_gaps": s["total_missing"],
            }
            report["gaps_sample"] = s["gaps"][:20]

            # ---- truncation ----
            last_ts = max((int(c[0]) for c in closed), default=None)
            reached_end = (
                last_ts is not None and last_ts >= win["eff_end_ms"]
            )

            if reached_end:
                truncated_by = None
            elif len(raw) >= args.max_candles:
                truncated_by = "max_candles"
            elif stats["logical_calls"] >= args.max_iterations:
                truncated_by = "max_iterations"
            else:
                truncated_by = "early_stop"
            report["truncation_reason"] = truncated_by

            # ---- completeness ----
            if (
                missing == 0
                and tsa["duplicates"] == 0
                and tsa["extra_count"] == 0
                and truncated_by is None
            ):
                report["completeness"] = "complete"
                report["has_known_gaps"] = False
            elif (
                missing > 0
                and len(unknown_missing_ts) == 0
                and tsa["duplicates"] == 0
                and tsa["extra_count"] == 0
                and truncated_by is None
            ):
                report["completeness"] = "complete_with_known_gaps"
                report["has_known_gaps"] = True
            else:
                report["completeness"] = "incomplete"
                report["has_known_gaps"] = len(known_missing_ts) > 0

            print("\n[Stage 4] HTTP instrumentation (retry counts estimated)...")
            print(f"  attempts_total          : {stats['attempts_total']}")
            print(f"  attempts_success        : {stats['attempts_success']}")
            print(f"  attempts_failed_429     : {stats['attempts_failed_429']}")
            print(f"  attempts_failed_5xx     : {stats['attempts_failed_5xx']}")
            print(f"  attempts_failed_timeout : "
                  f"{stats['attempts_failed_timeout']}")
            print(f"  attempts_failed_other   : "
                  f"{stats['attempts_failed_other']}")
            print(f"  logical_calls           : {stats['logical_calls']}")
            print(f"  retries_total           : {stats['retries_total']}")
            print(f"  retries_by_cause        : {stats['retries_by_cause']}")
            print(f"  method                  : {stats['method']}")
            print(f"  418 seen                : "
                  f"{stats['abort_reason'] == '418 received'}")

            print("\n[Stage 5] Last 3 candle closes:")
            for c in closed[-3:]:
                ts = int(c[0])
                close = float(c[4])
                iso_str = time.strftime(
                    "%Y-%m-%d %H:%M UTC", time.gmtime(ts / 1000),
                )
                print(f"  {iso_str}: {close}")

            # verdict_ok: برای complete و complete_with_known_gaps → True
            report["verdict_ok"] = report["completeness"] in (
                "complete", "complete_with_known_gaps",
            )
            exit_code = 0 if report["verdict_ok"] else 1

    except Exception as e:
        report["fatal_error"] = f"{type(e).__name__}: {e}"
        print(f"\n❌ FATAL: {report['fatal_error']}")
        exit_code = 1

    finally:
        if session is not None and report.get("http") is None:
            stats = classify_attempts(session.records)
            stats["session_aborted"] = session.aborted
            stats["abort_reason"] = session.abort_reason
            report["http"] = stats

        report["finished_at_iso"] = datetime.now(timezone.utc).isoformat()

        write_ok = _finalize_and_print(report, args.out)
        if not write_ok:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())