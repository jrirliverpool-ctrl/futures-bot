"""
probe_binance_history.py — تأیید دریافت داده‌ی تاریخی چندساله از Binance.

هدف (محدود و صریح):
  - اثبات قابلیت دریافت بازه‌ی تاریخی بلندمدت (چند ماه تا چند سال)
  - سنجش صحت صفحه‌بندی روی بازه‌های طولانی
  - شمارش retryها به تفکیک علت (429 / 5xx / timeout / other)
  - تشخیص دقیق نقص بازه بر پایه‌ی مجموعه‌ی timestampها
  - توقف فوری در صورت مشاهده‌ی HTTP 418 (مستقل از ماژول)

آنچه این Probe اثبات نمی‌کند:
  - کیفیت داده برای استراتژی (فقط پوشش و ساختار)
  - سودآوری (هیچ Backtest اجرا نمی‌شود)
  - تطابق با داده‌ی Toobit (BACKLOG-002)

هیچ Backtest اجرا نمی‌شود.
هیچ state ذخیره نمی‌شود.
هیچ سفارشی ارسال نمی‌شود.

قرارداد: این اسکریپت در همان پوشه‌ی binance_feed.py است (هر دو در scripts/).

نکات سازگاری با binance_feed.py:
  - end_ms در fetch_binance_klines_range یک مرز «شامل» است.
  - پاسخ خالی با strict_completeness=False → [] بدون استثنا.
  - 418 در _should_retry_http نیست → raise_for_status() آن را HTTPError می‌کند.

سه مفهوم جدا در گزارش:
  fetch_status       — نتیجه‌ی HTTP/exception در خود عملیات دریافت
  completeness       — پوشش داده، مستقل از موفقیت HTTP
  verdict_ok         — تنها وقتی True است که fetch_status == "ok"
                       و completeness == "complete"

v2 (نسبت به v1):
  1. 418 در InstrumentedSession مستقیماً RateLimitBan می‌اندازد
     (توقف قطعی، مستقل از رفتار binance_feed).
  2. شمارش retry با برچسب صریح «تخمینی» و مستندسازی فرض.
  3. تشخیص truncation بر پایه‌ی last_ts vs eff_end (به‌جای صرفاً
     len(raw) >= max_candles).
  4. محاسبه‌ی missing از تفاضل مجموعه‌ی timestampهای مورد انتظار و
     timestampهای واقعی (نه تفاضل تعداد ردیف‌ها).
  5. MIN_REQUEST_WINDOW_S صریح + عدم شروع درخواست زیر آستانه.
  6. --align {floor,ceil} با پیش‌فرض ceil.
  7. شکست نوشتن گزارش JSON → exit_code = 1.
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
# Constants
# ============================================================

MODE_PROFILES = {
    "short": {
        "fixed_start": None,               # offset-based
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

# آستانه‌ی حداقلی برای شروع یک درخواست جدید. اگر زمان باقی‌مانده کمتر از
# این مقدار باشد، هیچ درخواست جدیدی آغاز نمی‌شود و DeadlineExceeded صادر
# می‌گردد. این باعث می‌شود deadline واقعاً سخت‌گیرانه رعایت شود، نه اینکه
# با timeout کوتاه از آن عبور کنیم.
DEFAULT_MIN_REQUEST_WINDOW_S = 0.5

# برچسب روش شمارش retry. شمارش بر پایه‌ی trace جلسه تخمینی است و نه
# قطعی؛ چون به این فرض وابسته است که binance_feed پارامترهای درخواست را
# بین تلاش‌های مجدد حفظ می‌کند (که در نسخه‌ی فعلی همین‌طور است).
RETRY_COUNT_METHOD = "estimated_from_session_trace"


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
      - اعمال مهلت روی هر درخواست: timeout مؤثر = min(requested, remaining)
      - عدم شروع درخواست جدید اگر remaining < min_request_window_s
      - توقف فوری پس از 418: خود wrapper مستقیماً RateLimitBan می‌اندازد
        (مستقل از اینکه binance_feed چه می‌کند)
    """

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
            # ثبت شد؛ اکنون توقف قطعی، مستقل از ماژول.
            # این استثنا زیرکلاس ProbeAborted است، نه Timeout/ConnectionError؛
            # پس از try/except داخلی fetch_binance_klines عبور می‌کند و
            # مستقیماً به main() می‌رسد.
            self.aborted = True
            self.abort_reason = "418 received"
            raise RateLimitBan("418 received")

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


def _ceil_to_interval(ms: int, interval_ms: int) -> int:
    if ms % interval_ms == 0:
        return ms
    return ((ms // interval_ms) + 1) * interval_ms


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def resolve_window(start_iso, end_iso, interval, now_ms=None, align="ceil"):
    """
    بازه‌ی مؤثر = بازه‌ی کندل‌های *بسته*.

    align:
      - "ceil" (پیش‌فرض): اولین کندلی که openTime آن ≥ start_ms است.
        یعنی کندل‌های قبل از زمان درخواست وارد بازه نمی‌شوند.
      - "floor": آخرین کندلی که openTime آن ≤ start_ms است.
        یعنی کندل حاوی زمان شروع نیز وارد بازه می‌شود.

    قرارداد:
      - کندل با openTime = O زمانی بسته است که O + interval_ms <= now.
      - endTime برای binance_feed «شامل» است (ts <= end_ms).
      - برای مقاومت نسبت به رفتار API، مقدار ارسالی به ماژول
        eff_end + interval_ms است؛ فیلتر دقیق در Probe اعمال می‌شود.

    Returns dict: eff_start_ms, eff_end_ms, binance_end_ms, expected_count,
                  requested_start_ms, align
    """
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
# Timestamp set analysis
# ============================================================

def analyze_timestamps(closed_candles, eff_start_ms, eff_end_ms, interval):
    """
    تحلیل دقیق timestampها.

    missing از تفاضل مجموعه‌ی مورد انتظار و واقعی محاسبه می‌شود
    (نه از تفاضل تعداد ردیف‌ها)، بنابراین تکرار یا out-of-window
    نمی‌تواند آن را منحرف کند.

    Returns dict:
      duplicates, actual_unique, missing_count, missing_pct,
      missing_ranges (list of {start_iso, end_iso, count}),
      extra_count, aligned_actual_count
    """
    interval_ms = INTERVAL_MS[interval]
    expected_count = (eff_end_ms - eff_start_ms) // interval_ms + 1

    # مجموعه‌ی timestampهای واقعی + شمارش تکرارها
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

    # پیمایش ترتیبی روی موقعیت‌های مورد انتظار، استخراج بازه‌های پیوسته‌ی مفقود
    missing_ranges = []
    current_run_start = None
    missing_count = 0
    for i in range(expected_count):
        ts = eff_start_ms + i * interval_ms
        if ts not in actual_ts:
            if current_run_start is None:
                current_run_start = ts
            missing_count += 1
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

    missing_pct = (
        (missing_count / expected_count * 100) if expected_count else 0.0
    )

    # نمونه‌ی قابل‌چاپ برای گزارش
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
    }


# ============================================================
# Retry classification (estimated)
# ============================================================

def _classify_record(rec):
    if rec["outcome"] != "ok":
        return "timeout"        # شامل Timeout و ConnectionError
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
    """
    گروه‌بندی درخواست‌های متوالی به «فراخوان‌های منطقی».

    ⚠️ این شمارش *تخمینی* است. فرض می‌کند binance_feed پارامترهای
    درخواست را بین تلاش‌های مجدد حفظ می‌کند (که در نسخه‌ی فعلی همین است).
    اگر پارامترها بین retryها تغییر کنند، این تابع ممکن است یک فراخوان
    منطقی را به دو گروه بشکند یا برعکس.

    قاعده: یک فراخوان منطقی جدید شروع می‌شود اگر
      (الف) تلاش قبلی با 2xx تمام شده باشد، یا
      (ب) پارامترهای درخواست تغییر کرده باشند.
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
    p.add_argument("--start", default=None,
                   help="ISO UTC. اگر ندهی، بر اساس mode تعیین می‌شود.")
    p.add_argument("--end", default=None, help="ISO UTC. پیش‌فرض: اکنون.")
    p.add_argument("--align", default="ceil", choices=("floor", "ceil"),
                   help="نحوه‌ی تراز کردن start. پیش‌فرض: ceil")
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--max-seconds", type=int, default=None)
    p.add_argument("--min-request-window", type=float,
                   default=DEFAULT_MIN_REQUEST_WINDOW_S,
                   help="کمترین زمان لازم برای شروع یک درخواست جدید.")
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
        "version": "v2",
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
        },
        "window": None,
        "fetch": None,
        "counts": None,
        "gaps_sample": None,
        "http": None,
        "open_candles_removed": None,
        "dropped_out_of_window": None,
        "fetch_status": "not_started",     # HTTP/exception layer
        "completeness": "unknown",         # data quality layer
        "truncation_reason": None,         # max_candles|max_iterations|early_stop|None
        "verdict_ok": False,
        "fetch_error": None,
        "report_write_error": None,
        "fatal_error": None,
    }


def _print_header(args):
    print("=" * 72)
    print("🔍 PROBE BINANCE HISTORY — Historical Fetch Verification (v2)")
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
    print(f"  purpose    : تأیید پوشش تاریخی + صفحه‌بندی (نه Backtest)")
    print("=" * 72)


def _finalize_and_print(report, out_path):
    """
    چاپ خلاصه و نوشتن گزارش JSON. Returns bool (write_ok).
    """
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
    print(f"  missing_ranges   : {c.get('missing_ranges_total')} "
          f"(sample: {c.get('missing_ranges_sample')})")
    print(f"  duplicates       : {c.get('duplicates')}")
    print(f"  extra            : {c.get('extra')}")
    print(f"  gaps             : {c.get('gaps_total')} "
          f"(missing {c.get('total_missing_from_gaps')})")
    print(f"  open_removed     : {report.get('open_candles_removed')}")
    print(f"  dropped_oow      : {report.get('dropped_out_of_window')}")
    print(f"  fetch_status     : {report['fetch_status']}")
    print(f"  completeness     : {report.get('completeness', 'unknown')}")
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
    print("  ⚠️ مطابقت با Toobit و Commission نیازمند Probe جداگانه است.")
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

        # HTTP stats — همیشه ثبت می‌شوند
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

        # ---- خروج زودهنگام در صورت شکست fetch ----
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

            # ---- Stage 3b: timestamp set analysis (authoritative) ----
            print("\n  Timestamp set analysis (authoritative):")
            tsa = analyze_timestamps(
                closed,
                win["eff_start_ms"], win["eff_end_ms"], args.interval,
            )
            expected = win["expected_count"]
            actual_unique = tsa["actual_unique"]
            missing = tsa["missing_count"]

            print(f"    expected        : {expected}")
            print(f"    actual_unique   : {actual_unique}")
            print(f"    missing         : {missing} "
                  f"({tsa['missing_pct']:.4f}%)")
            print(f"    missing_ranges  : {tsa['missing_ranges_total']}")
            print(f"    duplicates      : {tsa['duplicates']}")
            print(f"    extra           : {tsa['extra_count']}")

            report["counts"] = {
                "expected": expected,
                "actual": len(closed),
                "actual_unique": actual_unique,
                "missing": missing,
                "missing_pct": tsa["missing_pct"],
                "missing_ranges_total": tsa["missing_ranges_total"],
                "missing_ranges_sample": tsa["missing_ranges_sample"],
                "duplicates": tsa["duplicates"],
                "extra": tsa["extra_count"],
                "first_iso": s["first_iso"],
                "last_iso": s["last_iso"],
                "gaps_total": s["gap_count"],
                "total_missing_from_gaps": s["total_missing"],
            }
            report["gaps_sample"] = s["gaps"][:20]

            # ---- تشخیص truncation بر پایه‌ی آخرین timestamp واقعی ----
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

            # ---- تعیین completeness ----
            if (
                missing == 0
                and tsa["duplicates"] == 0
                and tsa["extra_count"] == 0
                and truncated_by is None
            ):
                report["completeness"] = "complete"
            else:
                report["completeness"] = "incomplete"

            # ---- Stage 4: HTTP summary ----
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
            report["verdict_ok"] = (
                report["completeness"] == "complete"
            )
            exit_code = 0 if report["verdict_ok"] else 1

    except Exception as e:
        report["fatal_error"] = f"{type(e).__name__}: {e}"
        print(f"\n❌ FATAL: {report['fatal_error']}")
        exit_code = 1

    finally:
        # HTTP stats را حتی اگر Stage 3-5 اجرا نشده باشد پر کن
        if session is not None and report.get("http") is None:
            stats = classify_attempts(session.records)
            stats["session_aborted"] = session.aborted
            stats["abort_reason"] = session.abort_reason
            report["http"] = stats

        report["finished_at_iso"] = datetime.now(timezone.utc).isoformat()

        # نوشتن گزارش؛ اگر شکست خورد، exit_code را override کن
        write_ok = _finalize_and_print(report, args.out)
        if not write_ok:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())