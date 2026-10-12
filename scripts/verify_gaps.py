"""
verify_gaps.py — اعتبارسنجی مستقل بازه‌های مفقود در تاریخ بایننس.

هدف:
  - برای هر کندل مفقود، همان کندل را مستقیماً از data-api.binance.vision دوباره می‌پرسد.
  - دو کندل مرزی (قبل و بعد از هر بازه) را نیز بررسی می‌کند.
  - نتیجه‌ی هر پرسش:
      confirmed_present — API همان کندل را برگرداند
      api_empty         — API پاسخ خالی داد (شاهد، نه اثبات)
      boundary_mismatch — API timestamp متفاوتی برگرداند
      request_error     — خطای HTTP/شبکه
      aborted           — 418 یا اتمام مهلت

هیچ فایل مخزن را تغییر نمی‌دهد.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from binance_feed import (
    BASE_URL,
    KLINES_PATH,
    INTERVAL_MS,
    SUPPORTED_INTERVALS,
)


DEFAULT_MIN_INTERVAL_S = 0.25
DEFAULT_TIMEOUT_S = 15
DEFAULT_MAX_SECONDS = 300
DEFAULT_MAX_ATTEMPTS_429 = 3


class SessionAborted(Exception):
    pass


class VerifySession:
    def __init__(self, timeout=DEFAULT_TIMEOUT_S,
                 min_interval_s=DEFAULT_MIN_INTERVAL_S,
                 max_seconds=DEFAULT_MAX_SECONDS):
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self.deadline = (
            time.monotonic() + max_seconds if max_seconds > 0 else None
        )
        self.records = []
        self.aborted = False
        self.abort_reason = None
        self._last = 0.0

    def _throttle(self):
        if self.min_interval_s <= 0:
            return
        delta = time.monotonic() - self._last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)

    def _check_deadline(self):
        if self.deadline is None:
            return
        if time.monotonic() >= self.deadline:
            self.aborted = True
            self.abort_reason = self.abort_reason or "max_seconds exceeded"
            raise SessionAborted("max_seconds exceeded")

    def get(self, url, params=None, timeout=None):
        if self.aborted:
            raise SessionAborted(self.abort_reason or "session aborted")

        self._check_deadline()
        self._throttle()

        t0 = time.monotonic()
        try:
            r = requests.get(
                url, params=params, timeout=timeout or self.timeout,
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            self.records.append({
                "url": url,
                "params": dict(params) if params else {},
                "status": None,
                "outcome": type(exc).__name__,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            })
            self._last = time.monotonic()
            raise

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        self._last = time.monotonic()
        status = r.status_code

        self.records.append({
            "url": url,
            "params": dict(params) if params else {},
            "status": status,
            "outcome": "ok",
            "elapsed_ms": elapsed_ms,
            "retry_after": (r.headers or {}).get("Retry-After"),
        })

        if status == 418:
            self.aborted = True
            self.abort_reason = "418 received"

        return r


def _parse_iso_utc(s: str) -> int:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def load_gaps_from_report(path, symbol=None, interval=None):
    with open(path, "r", encoding="utf-8") as f:
        report = json.load(f)

    if symbol is not None and report.get("arguments", {}).get("symbol") != symbol:
        raise ValueError(f"Report symbol mismatch")
    if interval is not None and report.get("arguments", {}).get("interval") != interval:
        raise ValueError(f"Report interval mismatch")

    gaps_raw = report.get("gaps_sample")
    if not gaps_raw:
        return []

    report_interval = report["arguments"]["interval"]
    interval_ms = INTERVAL_MS[report_interval]

    gaps = []
    for g in gaps_raw:
        prev_ts = int(g["prev_ts"])
        next_ts = int(g["next_ts"])
        start_ms = prev_ts + interval_ms
        end_ms = next_ts - interval_ms
        count = int(g["missing_count"])

        expected_count = (end_ms - start_ms) // interval_ms + 1
        if expected_count != count:
            raise ValueError(f"Gap count mismatch")

        gaps.append({
            "after_index": g.get("after_index"),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_iso": _iso(start_ms),
            "end_iso": _iso(end_ms),
            "count": count,
            "prev_ts": prev_ts,
            "next_ts": next_ts,
        })

    return gaps


def query_candle(symbol, interval, ts, session,
                 max_attempts_429=DEFAULT_MAX_ATTEMPTS_429):
    interval_ms = INTERVAL_MS[interval]
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": ts,
        "endTime": ts + interval_ms - 1,
        "limit": 1,
    }
    url = f"{BASE_URL}{KLINES_PATH}"

    for attempt in range(max_attempts_429):
        try:
            r = session.get(url, params=params)
        except SessionAborted:
            return {"ts": ts, "iso": _iso(ts), "outcome": "aborted",
                    "error": session.abort_reason}
        except Exception as exc:
            return {"ts": ts, "iso": _iso(ts), "outcome": "request_error",
                    "error": f"{type(exc).__name__}: {exc}"}

        status = r.status_code

        if status == 418:
            return {"ts": ts, "iso": _iso(ts), "outcome": "aborted",
                    "error": "418 received"}

        if status == 429:
            retry_after = (r.headers or {}).get("Retry-After")
            if attempt < max_attempts_429 - 1:
                wait_s = (float(retry_after) if retry_after is not None
                          else (1.0 * (2 ** attempt)))
                time.sleep(wait_s)
                continue
            return {"ts": ts, "iso": _iso(ts), "outcome": "request_error",
                    "error": f"429 after {max_attempts_429} attempts"}

        if status >= 400:
            return {"ts": ts, "iso": _iso(ts), "outcome": "request_error",
                    "error": f"HTTP {status}"}

        try:
            data = r.json()
        except ValueError:
            return {"ts": ts, "iso": _iso(ts), "outcome": "request_error",
                    "error": "invalid JSON"}

        if not isinstance(data, list):
            return {"ts": ts, "iso": _iso(ts), "outcome": "request_error",
                    "error": "unexpected response type"}

        if len(data) == 0:
            return {"ts": ts, "iso": _iso(ts), "outcome": "api_empty"}

        returned_ts = int(data[0][0])
        if returned_ts == ts:
            return {"ts": ts, "iso": _iso(ts), "outcome": "confirmed_present"}

        return {"ts": ts, "iso": _iso(ts), "outcome": "boundary_mismatch",
                "expected_ts": ts, "returned_ts": returned_ts,
                "returned_iso": _iso(returned_ts)}

    return {"ts": ts, "iso": _iso(ts), "outcome": "request_error",
            "error": "unexpected end of retry loop"}


def compute_verdict(gap, prev, next_, candle_results):
    cm = sum(1 for c in candle_results if c["outcome"] == "api_empty")
    cp = sum(1 for c in candle_results if c["outcome"] == "confirmed_present")
    err = sum(1 for c in candle_results if c["outcome"] == "request_error")
    bm = sum(1 for c in candle_results if c["outcome"] == "boundary_mismatch")
    ab = sum(1 for c in candle_results if c["outcome"] == "aborted")

    if ab > 0 or err > 0 or bm > 0:
        return "unresolved"

    prev_ok = prev["outcome"] == "confirmed_present"
    next_ok = next_["outcome"] == "confirmed_present"
    if not prev_ok or not next_ok:
        return "boundary_suspect"

    original_count = gap["count"]
    if cm == original_count and cp == 0:
        return "confirmed_gap"
    if cp == original_count and cm == 0:
        return "disputed_present"
    if cm + cp == original_count:
        return "partial_mismatch"
    return "unresolved"


def _parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1h", choices=SUPPORTED_INTERVALS)
    p.add_argument("--gaps-from", required=True)
    p.add_argument("--out", default="reports/verify_gaps_report.json")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--min-interval-s", type=float, default=DEFAULT_MIN_INTERVAL_S)
    p.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS)
    p.add_argument("--max-attempts-429", type=int, default=DEFAULT_MAX_ATTEMPTS_429)
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    print("=" * 72)
    print("VERIFY GAPS — Independent Candle Query")
    print("=" * 72)
    print(f"  symbol     : {args.symbol}")
    print(f"  interval   : {args.interval}")
    print(f"  source     : {BASE_URL}{KLINES_PATH}")
    print(f"  gaps_from  : {args.gaps_from}")
    print(f"  out        : {args.out}")
    print("=" * 72)

    try:
        gaps = load_gaps_from_report(
            args.gaps_from, symbol=args.symbol, interval=args.interval,
        )
    except Exception as e:
        print(f"\nFAILED to load gaps: {type(e).__name__}: {e}")
        return 1

    if not gaps:
        print("\nNo gaps to verify")
        return 0

    total_missing = sum(g["count"] for g in gaps)
    print(f"\n  gaps loaded  : {len(gaps)}")
    print(f"  missing total: {total_missing}")
    print(f"  queries est. : {total_missing + 2 * len(gaps)}")

    session = VerifySession(
        timeout=args.timeout,
        min_interval_s=args.min_interval_s,
        max_seconds=args.max_seconds,
    )

    gap_reports = []

    try:
        for i, gap in enumerate(gaps):
            if session.aborted:
                break

            print(f"\n[Gap {i+1}/{len(gaps)}] "
                  f"{gap['start_iso']} -> {gap['end_iso']} ({gap['count']})")

            prev_ts = gap["start_ms"] - INTERVAL_MS[args.interval]
            prev_result = query_candle(
                args.symbol, args.interval, prev_ts, session,
                max_attempts_429=args.max_attempts_429,
            )
            print(f"  prev  {prev_result['iso']}: {prev_result['outcome']}")

            next_ts = gap["end_ms"] + INTERVAL_MS[args.interval]
            next_result = query_candle(
                args.symbol, args.interval, next_ts, session,
                max_attempts_429=args.max_attempts_429,
            )
            print(f"  next  {next_result['iso']}: {next_result['outcome']}")

            candle_results = []
            ts = gap["start_ms"]
            interval_ms = INTERVAL_MS[args.interval]
            while ts <= gap["end_ms"]:
                res = query_candle(
                    args.symbol, args.interval, ts, session,
                    max_attempts_429=args.max_attempts_429,
                )
                candle_results.append(res)
                if res["outcome"] == "aborted":
                    break
                ts += interval_ms

            verdict = compute_verdict(
                gap, prev_result, next_result, candle_results,
            )
            print(f"  verdict: {verdict}")

            gap_reports.append({
                "gap_index": i,
                "original": {
                    "start_iso": gap["start_iso"],
                    "end_iso": gap["end_iso"],
                    "count": gap["count"],
                    "after_index": gap.get("after_index"),
                },
                "boundary_prev": prev_result,
                "boundary_next": next_result,
                "candles": candle_results,
                "confirmed_missing_count": sum(
                    1 for c in candle_results if c["outcome"] == "api_empty"
                ),
                "confirmed_present_count": sum(
                    1 for c in candle_results if c["outcome"] == "confirmed_present"
                ),
                "request_error_count": sum(
                    1 for c in candle_results if c["outcome"] == "request_error"
                ),
                "boundary_mismatch_count": sum(
                    1 for c in candle_results if c["outcome"] == "boundary_mismatch"
                ),
                "verdict": verdict,
            })

    except Exception as e:
        print(f"\nFATAL: {type(e).__name__}: {e}")

    verdicts = {}
    for gr in gap_reports:
        verdicts[gr["verdict"]] = verdicts.get(gr["verdict"], 0) + 1

    total_confirmed_missing = sum(gr["confirmed_missing_count"] for gr in gap_reports)
    total_confirmed_present = sum(gr["confirmed_present_count"] for gr in gap_reports)
    total_errors = sum(gr["request_error_count"] for gr in gap_reports)
    total_bm = sum(gr["boundary_mismatch_count"] for gr in gap_reports)

    report = {
        "probe": "verify_gaps",
        "version": "v1",
        "started_at_iso": datetime.now(timezone.utc).isoformat(),
        "finished_at_iso": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol,
        "interval": args.interval,
        "source": BASE_URL,
        "source_url": f"{BASE_URL}{KLINES_PATH}",
        "input_gaps_file": args.gaps_from,
        "arguments": {
            "timeout": args.timeout,
            "min_interval_s": args.min_interval_s,
            "max_seconds": args.max_seconds,
            "max_attempts_429": args.max_attempts_429,
        },
        "summary": {
            "gaps_total": len(gaps),
            "gaps_processed": len(gap_reports),
            "candles_queried": sum(len(gr["candles"]) for gr in gap_reports),
            "boundaries_queried": 2 * len(gap_reports),
            "confirmed_missing_total": total_confirmed_missing,
            "confirmed_present_total": total_confirmed_present,
            "request_errors_total": total_errors,
            "boundary_mismatch_total": total_bm,
            "verdicts": verdicts,
        },
        "gaps": gap_reports,
        "aborted": session.aborted,
        "abort_reason": session.abort_reason,
        "http": {"total_requests": len(session.records)},
    }

    print()
    print("=" * 72)
    print("VERIFY GAPS - SUMMARY")
    print("=" * 72)
    print(f"  gaps_total              : {report['summary']['gaps_total']}")
    print(f"  gaps_processed          : {report['summary']['gaps_processed']}")
    print(f"  candles_queried         : {report['summary']['candles_queried']}")
    print(f"  boundaries_queried      : {report['summary']['boundaries_queried']}")
    print(f"  confirmed_missing_total : {total_confirmed_missing}")
    print(f"  confirmed_present_total : {total_confirmed_present}")
    print(f"  request_errors_total    : {total_errors}")
    print(f"  boundary_mismatch_total : {total_bm}")
    print(f"  verdicts                : {verdicts}")
    print(f"  total HTTP requests     : {report['http']['total_requests']}")
    if session.aborted:
        print(f"  ABORTED                 : {session.abort_reason}")
    print("=" * 72)

    write_ok = True
    try:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n  report written to {args.out}")
    except Exception as e:
        print(f"\n  failed to write report: {type(e).__name__}: {e}")
        write_ok = False

    if not write_ok:
        return 1
    if verdicts.get("unresolved", 0) > 0 or verdicts.get("boundary_suspect", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())