"""
probe_binance_history.py — تأیید دریافت داده‌ی تاریخی چندساله از Binance.

هدف (محدود و صریح):
  - اثبات قابلیت دریافت بازه‌ی تاریخی بلندمدت (چند ماه تا چند سال)
  - سنجش صحت صفحه‌بندی روی بازه‌های طولانی
  - شمارش دقیق retryها به تفکیک علت (429 / 5xx / timeout / other)
  - تشخیص دقیق نقص بازه (expected vs actual) و تفکیک gap از truncation
  - توقف فوری در صورت مشاهده‌ی HTTP 418

آنچه این Probe اثبات نمی‌کند:
  - کیفیت داده برای استراتژی (فقط پوشش و ساختار)
  - سودآوری (هیچ Backtest اجرا نمی‌شود)
  - تطابق با داده‌ی Toobit (BACKLOG-002)

هیچ Backtest اجرا نمی‌شود.
هیچ state ذخیره نمی‌شود.
هیچ سفارشی ارسال نمی‌شود.

قرارداد: این اسکریپت در همان پوشه‌ی binance_feed.py است (هر دو در scripts/).

نکته‌ی سازگاری با binance_feed.py (تأییدشده با tests/test_binance_feed.py):
  - end_ms در fetch_binance_klines_range یک مرز «شامل» است
    (ts <= end_ms پذیرفته می‌شود).
  - پاسخ خالی با strict_completeness=False → [] بدون استثنا.
  - 418 در _should_retry_http نیست → raise_for_status() آن را HTTPError می‌کند.
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


# ============================================================
# Mode profiles
# ============================================================

MODE_PROFILES = {
    "short": {
        "fixed_start": None,          # offset-based
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


# ============================================================
# Exceptions
# ============================================================

class ProbeAborted(Exception):
    """توقف عمدی Probe (418 یا اتمام مهلت)."""


class DeadlineExceeded(ProbeAborted):
    """مهلت زمانی اجرا تمام شد."""


class RateLimitBan(ProbeAborted):
    """HTTP 418 مشاهده شد — هیچ درخواست دیگری ارسال نمی‌شود."""


# ============================================================
# Instrumented session
# ============================================================

class InstrumentedSession:
    """
    Wrapper روی requests (یا session تزریقی).

    ویژگی‌ها:
      - ثبت هر درخواست: URL، params، outcome، status، latency، Retry-After
      - اعمال مهلت روی هر درخواست: timeout مؤثر = min(timeout, remaining)
      - توقف فوری پس از 418: get بعدی بلافاصله RateLimitBan می‌اندازد
    """

    def __init__(self, inner=None, deadline_monotonic=None):
        self.inner = inner if inner is not None else requests
        self.deadline_monotonic = deadline_monotonic
        self.records = []
        self.aborted = False
        self.abort_reason = None

    def _effective_timeout(self, requested):
        if self.deadline_monotonic is None:
            return requested
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            self.aborted = True
            self.abort_reason = self.abort_reason or "max_seconds exceeded"
            raise DeadlineExceeded("probe max_seconds exceeded")
        base = float(requested) if requested is not None else 15.0
        # کف کوچک برای اینکه requests مقدار 0 نگیرد؛ اگر باقی‌مانده
        # از این کف کمتر باشد، عملاً درخواست بعدی timeout می‌شود و
        # حلقه در همان نقطه متوقف می‌گردد.
        floor = 0.1
        return max(floor, min(base, remaining))

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

        return response


# ============================================================
# Time helpers
# ============================================================

def _parse_iso_utc(s: str) -> int:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _floor_to_interval(ms: int, interval_ms: int) -> int:
    return (ms // interval_ms) * interval_ms


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def resolve_window(start_iso, end_iso, interval, now_ms=None):
    """
    بازه‌ی مؤثر = بازه‌ی کندل‌های *بسته*.

    قرارداد:
      - کندل با openTime = O زمانی بسته است که O + interval_ms <= now.
      - endTime برای ماژول binance_feed «شامل» است (ts <= end_ms).
      - برای مقاومت نسبت به رفتار API، مقدار ارسالی به ماژول را
        eff_end + interval_ms می‌گذاریم و در Probe بر اساس
        [eff_start, eff_end] فیلتر دقیق اعمال می‌کنیم. اگر API یک
        کندل اضافه برگرداند، Stage 3 آن را حذف می‌کند.

    Returns dict با کلیدهای:
      eff_start_ms, eff_end_ms, binance_end_ms, expected_count
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    interval_ms = INTERVAL_MS[interval]
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    start_ms = _parse_iso_utc(start_iso)
    requested_end = _parse_iso_utc(end_iso) if end_iso else now_ms
    requested_end = min(requested_end, now_ms)

    eff_start_ms = _floor_to_interval(start_ms, interval_ms)

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
    }


# ============================================================
# Retry classification
# ============================================================

def _classify_record(rec):
    """برچسب علت شکست هر درخواست."""
    if rec["outcome"] != "ok":
        return "timeout"        # شامل Timeout و ConnectionError
    s = rec["status"]
    if s == 429:
        return "429"
    if s is not None and 500 <= s <= 599:
        return "5xx"
    return "other"


def classify_attempts(records):
    """
    درخواست‌های متوالی با params یکسان = یک «فراخوان منطقی» (صفحه).
    retry = هر تلاش شکست‌خورده که یک تلاش بعدی در همان گروه دارد.

    Returns dict با کلیدهای:
      attempts_total, attempts_success,
      attempts_failed_429, attempts_failed_5xx,
      attempts_failed_timeout, attempts_failed_other,
      logical_calls, retries_total,
      retries_by_cause = {429, 5xx, timeout, other}
    """
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
    }
    if not records:
        return empty

    # گروه‌بندی consecutive records با params یکسان
    groups = []
    current = [records[0]]
    for rec in records[1:]:
        if rec["params"] == current[-1]["params"]:
            current.append(rec)
        else:
            groups.append(current)
            current = [rec]
    groups.append(current)

    attempts_success = 0
    failed = Counter()
    retries_by_cause = Counter()

    for g in groups:
        for r in g:
            if r["outcome"] == "ok" and 200 <= (r["status"] or 0) < 300:
                attempts_success += 1
            else:
                failed[_classify_record(r)] += 1
        # retry = هر تلاش به‌جز آخرین تلاش گروه
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
    p.add_argument("--start", default=None,
                   help="ISO UTC. اگر ندهی، بر اساس mode تعیین می‌شود.")
    p.add_argument("--end", default=None, help="ISO UTC. پیش‌فرض: اکنون.")
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--max-seconds", type=int, default=None)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--backoff-base", type=float, default=0.5)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--sleep-between", type=float, default=0.15)
    p.add_argument("--out", default=None,
                   help="مسیر فایل JSON گزارش. اگر ندهی، فقط stdout.")

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
        "started_at_iso": datetime.now(timezone.utc).isoformat(),
        "arguments": {
            "symbol": args.symbol,
            "interval": args.interval,
            "mode": args.mode,
            "start": args.start,
            "end": args.end,
            "max_candles": args.max_candles,
            "max_iterations": args.max_iterations,
            "max_seconds": args.max_seconds,
            "max_retries": args.max_retries,
            "backoff_base": args.backoff_base,
            "timeout": args.timeout,
        },
        "window": None,
        "fetch": None,
        "counts": None,
        "gaps_sample": None,
        "http": None,
        "open_candles_removed": None,
        "dropped_out_of_window": None,
        "fetch_status": "not_started",
        "fetch_error": None,
        "fatal_error": None,
        "verdict_ok": False,
    }


def _print_header(args):
    print("=" * 72)
    print("🔍 PROBE BINANCE HISTORY — Historical Fetch Verification")
    print("=" * 72)
    print(f"  symbol     : {args.symbol}")
    print(f"  interval   : {args.interval}")
    print(f"  mode       : {args.mode}")
    print(f"  start      : {args.start}")
    print(f"  end        : {args.end or 'now'}")
    print(f"  max_candles: {args.max_candles}")
    print(f"  max_iter   : {args.max_iterations}")
    print(f"  max_seconds: {args.max_seconds}")
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

    print(f"  mode             : {report['arguments']['mode']}")
    print(f"  symbol/interval  : "
          f"{report['arguments']['symbol']} / {report['arguments']['interval']}")
    print(f"  range            : {w.get('effective_start_iso')} → "
          f"{w.get('effective_end_iso')}")
    print(f"  expected/actual  : {w.get('expected_count')}/{c.get('actual')}")
    print(f"  missing          : {c.get('missing')}")
    print(f"  duplicates       : {c.get('duplicates')}")
    print(f"  gaps             : {c.get('gaps_total')} "
          f"(missing {c.get('total_missing_from_gaps')})")
    print(f"  open_removed     : {report.get('open_candles_removed')}")
    print(f"  dropped_oow      : {report.get('dropped_out_of_window')}")
    print(f"  fetch_status     : {report['fetch_status']}")
    print(f"  requests         : {h.get('attempts_total')} "
          f"(retries: {h.get('retries_total')})")
    print(f"  retries_by_cause : {h.get('retries_by_cause')}")
    print(f"  418 seen         : "
          f"{h.get('abort_reason') == '418 received'}")
    print(f"  wall_clock_s     : {f.get('wall_clock_s')}")
    if report.get("fetch_error"):
        print(f"  fetch_error      : {report['fetch_error']}")
    if report.get("fatal_error"):
        print(f"  fatal_error      : {report['fatal_error']}")

    print()
    print("  ⚠️ این Probe فقط پوشش و ساختار داده را تأیید می‌کند.")
    print("  ⚠️ اثبات سودآوری استراتژی نیست و Backtest اجرا نمی‌کند.")
    print("  ⚠️ مطابقت با Toobit و Commission نیازمند Probe جداگانه است.")
    print("=" * 72)

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
            print(f"\n  ❌ failed to write report: "
                  f"{type(e).__name__}: {e}")


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
        # ---- Stage 1: window ----
        print("\n[Stage 1] Resolving effective window (closed candles only)...")
        win = resolve_window(args.start, args.end, args.interval)
        report["window"] = {
            "effective_start_iso": _iso(win["eff_start_ms"]),
            "effective_end_iso": _iso(win["eff_end_ms"]),
            "effective_start_ms": win["eff_start_ms"],
            "effective_end_ms": win["eff_end_ms"],
            "expected_count": win["expected_count"],
        }
        print(f"  eff_start_iso  : {_iso(win['eff_start_ms'])}")
        print(f"  eff_end_iso    : {_iso(win['eff_end_ms'])}")
        print(f"  expected_count : {win['expected_count']}")
        print("  ✅ window resolved")

        # ---- Stage 2: fetch ----
        print("\n[Stage 2] Fetching range (strict_completeness=False)...")
        deadline = time.monotonic() + args.max_seconds
        session = InstrumentedSession(deadline_monotonic=deadline)

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

        # HTTP stats — همیشه ثبت می‌شوند، حتی در شکست
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

        # ---- چنانچه fetch شکست خورده، Stage 3-5 را رد کن ----
        if aborted_reason is not None or report["fetch_status"] in (
            "aborted_418", "aborted_deadline", "http_error",
            "truncated_module_error", "invalid_candle", "error",
        ):
            report["verdict_ok"] = False
            exit_code = 1
            return exit_code

        # اگر پاسخ خالی بود، وضعیت صریح ثبت کن
        if not raw:
            report["fetch_status"] = "empty_response"
            report["verdict_ok"] = False
            exit_code = 1
            return exit_code

        # ---- Stage 3: filter + diagnostics ----
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

        print("\n  Structural diagnostics:")
        s = summarize_candles(closed, args.interval)
        actual = s["count"]
        expected = win["expected_count"]
        missing = expected - actual

        print(f"    count       : {s['count']}")
        print(f"    first_iso   : {s['first_iso']}")
        print(f"    last_iso    : {s['last_iso']}")
        print(f"    duplicates  : {s['duplicates']}")
        print(f"    gap_count   : {s['gap_count']}")
        print(f"    total_miss  : {s['total_missing']}")
        print(f"    expected    : {expected}")
        print(f"    actual      : {actual}")
        print(f"    missing     : {missing}")

        report["counts"] = {
            "expected": expected,
            "actual": actual,
            "missing": missing,
            "duplicates": s["duplicates"],
            "first_iso": s["first_iso"],
            "last_iso": s["last_iso"],
            "gaps_total": s["gap_count"],
            "total_missing_from_gaps": s["total_missing"],
        }
        report["gaps_sample"] = s["gaps"][:20]

        # ---- fetch_status نهایی ----
        truncated_by_module = (
            len(raw) >= args.max_candles
            or stats["logical_calls"] >= args.max_iterations
        )

        if truncated_by_module:
            report["fetch_status"] = "truncated_max_candles"
        elif missing == 0 and s["duplicates"] == 0:
            report["fetch_status"] = "complete"
        else:
            report["fetch_status"] = "incomplete"

        # ---- Stage 4: HTTP summary ----
        print("\n[Stage 4] HTTP instrumentation...")
        print(f"  attempts_total          : {stats['attempts_total']}")
        print(f"  attempts_success        : {stats['attempts_success']}")
        print(f"  attempts_failed_429     : {stats['attempts_failed_429']}")
        print(f"  attempts_failed_5xx     : {stats['attempts_failed_5xx']}")
        print(f"  attempts_failed_timeout : {stats['attempts_failed_timeout']}")
        print(f"  attempts_failed_other   : {stats['attempts_failed_other']}")
        print(f"  logical_calls           : {stats['logical_calls']}")
        print(f"  retries_total           : {stats['retries_total']}")
        print(f"  retries_by_cause        : {stats['retries_by_cause']}")
        print(f"  418 seen                : "
              f"{stats['abort_reason'] == '418 received'}")

        # ---- Stage 5: sample prices ----
        print("\n[Stage 5] Last 3 candle closes:")
        for c in closed[-3:]:
            ts = int(c[0])
            close = float(c[4])
            iso_str = time.strftime(
                "%Y-%m-%d %H:%M UTC", time.gmtime(ts / 1000),
            )
            print(f"  {iso_str}: {close}")

        # ---- Verdict ----
        report["verdict_ok"] = (report["fetch_status"] == "complete")
        exit_code = 0 if report["verdict_ok"] else 1

    except Exception as e:
        report["fatal_error"] = f"{type(e).__name__}: {e}"
        print(f"\n❌ FATAL: {report['fatal_error']}")
        exit_code = 1

    finally:
        # گزارش HTTP را حتی اگر Stage 3-5 اجرا نشده باشد پر کن
        if session is not None and report.get("http") is None:
            stats = classify_attempts(session.records)
            stats["session_aborted"] = session.aborted
            stats["abort_reason"] = session.abort_reason
            report["http"] = stats

        report["finished_at_iso"] = datetime.now(timezone.utc).isoformat()
        _finalize_and_print(report, args.out)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())