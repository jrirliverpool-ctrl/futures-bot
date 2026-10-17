"""
probe_toobit_funding.py — کشف ساختار و کیفیت داده‌ی funding از Toobit.

v1.1 — اصلاحات نسبت به v1.0:

  1. تفکیک سه وضعیت مستقل:
       endpoint_ok          — آیا endpoint پاسخ معتبر داد؟
       pagination_verified  — آیا رفتار cursor تأیید شد؟
       data_quality_ok      — آیا داده کافی/سالم است؟
     exit code فقط بر اساس endpoint_ok.
     موفقیت در endpoint ≠ موفقیت در داده.

  2. ردیابی ID های تکراری در سراسر صفحات.
     شمارش unique از total.

  3. نام‌گذاری صریح: oldest_settle_utc / newest_settle_utc
     (نه first/last — چون ترتیب منبع نامشخص است).
     ثبت ترتیب منبع (source_order_is_sorted_asc/desc).

  4. تحلیل delta با فراوانی و شناسایی delta غیرمنتظره.
     شکاف ۱۶ ساعته در داده‌ی ۸ ساعته = هشدار.
     شمارش invalid_records (نرخ غیرعددی، timestamp نامعتبر).

  5. ذخیره‌ی پاسخ خام هر صفحه در گزارش برای بازبینی.

  6. تأیید URL واقعی پاسخ (نه فقط ثابت BaseURL).

آنچه این probe اثبات نمی‌کند:
  - کیفیت نهایی داده برای محاسبه‌ی PnL
  - سازگاری با paper_engine
  - پوشش کامل بازه‌ی Training/OOS

هیچ تغییری در state، events، یا کد تولیدی ایجاد نمی‌شود.
فقط GET به endpoint عمومی Toobit.
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests


BASE_URL = "https://api.toobit.com"
FUNDING_PATH = "/api/v1/futures/historyFundingRate"

DEFAULT_SYMBOL = "BTC-SWAP-USDT"
DEFAULT_LIMIT = 100
DEFAULT_MAX_PAGES = 5


# ============================================================
# Parsing helpers
# ============================================================

def _iso(ms):
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError, TypeError):
        return ""


def _parse_settle_time(item):
    """
    settleTime ممکن است ms یا seconds باشد.
    Returns: int (ms) یا None اگر نامعتبر بود.
    """
    t = item.get("settleTime")
    if t is None:
        return None
    try:
        t = int(t)
    except (ValueError, TypeError):
        return None
    # اگر کوچک‌تر از 1e12 بود، احتمالاً ثانیه است
    if t < 10_000_000_000:
        t = t * 1000
    return t


def _extract_item_id(item):
    """آیتم ID را از فیلدهای محتمل استخراج می‌کند."""
    for key in ("id", "fundingId", "rateId", "settleId"):
        if key in item and item[key] is not None:
            try:
                return int(item[key])
            except (ValueError, TypeError):
                return None
    return None


def _extract_items(response_json):
    """
    استخراج لیست آیتم‌ها از پاسخ.
    Toobit ممکن است data/result/items/list استفاده کند.
    """
    if isinstance(response_json, list):
        return response_json
    if isinstance(response_json, dict):
        for key in ("data", "result", "items", "list"):
            val = response_json.get(key)
            if isinstance(val, list):
                return val
    raise ValueError(
        f"Unexpected response structure: {type(response_json).__name__}"
    )


# ============================================================
# HTTP
# ============================================================

def fetch_funding_page(url, symbol, from_id=None, limit=100,
                       session=None, timeout=15):
    """
    یک صفحه از تاریخچه‌ی funding.

    Returns: dict با فیلدهای:
      http_status     : int یا None
      request_url     : URL واقعی که پاسخ داد
      response_json   : پاسخ خام JSON (یا None)
      items           : لیست آیتم‌ها (یا None)
      error           : پیام خطا (یا None)
    """
    params = {"symbol": symbol, "limit": limit}
    if from_id is not None:
        params["fromId"] = from_id

    client = session if session is not None else requests

    result = {
        "http_status": None,
        "request_url": None,
        "response_json": None,
        "items": None,
        "error": None,
        "from_id_used": from_id,
    }

    try:
        r = client.get(url, params=params, timeout=timeout)
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError) as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result["http_status"] = r.status_code
    result["request_url"] = r.url

    try:
        result["response_json"] = r.json()
    except ValueError as e:
        result["error"] = f"invalid JSON: {e}"
        return result

    if r.status_code >= 400:
        result["error"] = f"HTTP {r.status_code}"
        return result

    try:
        result["items"] = _extract_items(result["response_json"])
    except ValueError as e:
        result["error"] = str(e)
        return result

    return result


# ============================================================
# Analysis
# ============================================================

def analyze_records(all_items, all_ids_across_pages):
    """
    تحلیل آیتم‌های دریافت‌شده.

    all_items: لیست خام آیتم‌ها به ترتیبی که از API رسیدند.
    all_ids_across_pages: لیست ID ها به ترتیب دریافت (شامل تکراری‌ها).

    Returns: dict با تحلیل کامل.
    """
    source_order_ts = []   # timestamps به ترتیب منبع
    valid_rates = []
    invalid_ts_count = 0
    invalid_rate_count = 0
    missing_field_count = 0

    for it in all_items:
        if not isinstance(it, dict):
            missing_field_count += 1
            continue

        ts = _parse_settle_time(it)
        if ts is None:
            invalid_ts_count += 1
        else:
            source_order_ts.append(ts)

        rate = it.get("settleRate")
        if rate is None:
            invalid_rate_count += 1
        else:
            try:
                valid_rates.append(float(rate))
            except (ValueError, TypeError):
                invalid_rate_count += 1

    sorted_ts = sorted(source_order_ts)

    # delta بین تسویه‌های متوالی (روی زمان‌های مرتب‌شده)
    deltas_h = []
    for i in range(1, len(sorted_ts)):
        d_ms = sorted_ts[i] - sorted_ts[i - 1]
        deltas_h.append(round(d_ms / 3_600_000, 2))

    delta_freq = Counter(deltas_h)
    modal_delta = None
    if delta_freq:
        modal_delta = delta_freq.most_common(1)[0][0]

    unexpected = {
        str(d): c for d, c in delta_freq.items() if d != modal_delta
    }

    # rate stats
    rate_stats = {}
    if valid_rates:
        rate_stats = {
            "count": len(valid_rates),
            "min": min(valid_rates),
            "max": max(valid_rates),
            "mean": sum(valid_rates) / len(valid_rates),
            "positive_count": sum(1 for r in valid_rates if r > 0),
            "negative_count": sum(1 for r in valid_rates if r < 0),
            "zero_count": sum(1 for r in valid_rates if r == 0),
        }

    # تکرار ID ها (در سراسر صفحات)
    id_counter = Counter(all_ids_across_pages)
    duplicate_ids = {
        str(k): v for k, v in id_counter.items() if v > 1
    }

    # ترتیب زمانی منبع
    is_asc = source_order_ts == sorted_ts if source_order_ts else None
    is_desc = (
        source_order_ts == list(reversed(sorted_ts))
        if source_order_ts else None
    )

    return {
        "total_items": len(all_items),
        "unique_settle_times": len(set(source_order_ts)),
        "source_order_is_ascending": is_asc,
        "source_order_is_descending": is_desc,
        "oldest_settle_ms": sorted_ts[0] if sorted_ts else None,
        "newest_settle_ms": sorted_ts[-1] if sorted_ts else None,
        "oldest_settle_utc": _iso(sorted_ts[0]) if sorted_ts else "",
        "newest_settle_utc": _iso(sorted_ts[-1]) if sorted_ts else "",
        "delta_stats": {
            "unique_deltas_hours": sorted(delta_freq.keys()),
            "delta_frequency": {str(k): v for k, v in delta_freq.items()},
            "modal_delta_hours": modal_delta,
            "unexpected_deltas": unexpected,
        },
        "rate_stats": rate_stats,
        "invalid_records": {
            "invalid_timestamps": invalid_ts_count,
            "invalid_rates": invalid_rate_count,
            "missing_fields": missing_field_count,
        },
        "duplicate_ids": duplicate_ids,
        "unique_id_count": len(id_counter),
        "total_id_count": len(all_ids_across_pages),
    }


# ============================================================
# Main
# ============================================================

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Probe Toobit funding rate history (read-only).",
    )
    p.add_argument("--symbol", default=DEFAULT_SYMBOL)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    p.add_argument("--max-seconds", type=float, default=60.0)
    p.add_argument("--sleep-between", type=float, default=0.3)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    url = f"{BASE_URL}{FUNDING_PATH}"

    print("=" * 72)
    print("🔍 PROBE TOOBIT FUNDING — Read-Only (v1.1)")
    print("=" * 72)
    print(f"  target_url  : {url}")
    print(f"  symbol      : {args.symbol}")
    print(f"  limit/page  : {args.limit}")
    print(f"  max_pages   : {args.max_pages}")
    print(f"  max_seconds : {args.max_seconds}")
    print("=" * 72)
    print()

    deadline = time.monotonic() + args.max_seconds

    # وضعیت‌ها
    endpoint_ok = False
    pagination_verified = False
    data_quality_ok = None       # نامعلوم تا تحلیل نهایی

    # جمع‌آوری
    all_items = []
    all_ids_across_pages = []
    page_records = []
    errors = []
    actual_response_url = None

    session = requests.Session()
    cursor_from_id = None
    page = 0

    try:
        while page < args.max_pages:
            if time.monotonic() > deadline:
                print(f"⏱  max_seconds reached after {page} pages")
                break

            page += 1
            print(f"[Page {page}] fetching fromId={cursor_from_id}...")

            rec = fetch_funding_page(
                url, args.symbol,
                from_id=cursor_from_id,
                limit=args.limit,
                session=session,
            )

            page_record = {
                "page": page,
                "http_status": rec["http_status"],
                "request_url": rec["request_url"],
                "from_id_used": rec["from_id_used"],
                "items_returned": (
                    len(rec["items"]) if rec["items"] is not None else None
                ),
                "error": rec["error"],
                "response_json_raw": rec["response_json"],
            }
            page_records.append(page_record)

            if rec["request_url"]:
                actual_response_url = rec["request_url"].split("?")[0]

            if rec["error"]:
                print(f"  ❌ {rec['error']}")
                errors.append({"page": page, "error": rec["error"]})
                break

            # endpoint_ok فقط وقتی که پاسخ معتبر و ساختار درست بود
            endpoint_ok = True

            items = rec["items"]
            print(f"  received {len(items)} items")

            if not items:
                print("  → empty page, stop")
                break

            # ثبت آیتم‌ها و ID ها
            for it in items:
                all_items.append(it)
                item_id = _extract_item_id(it) if isinstance(it, dict) else None
                all_ids_across_pages.append(item_id if item_id is not None else "")

            # تعیین cursor برای صفحه‌ی بعد
            last = items[-1] if items else None
            next_cursor = _extract_item_id(last) if isinstance(last, dict) else None

            if next_cursor is None:
                print("  ⚠️  no usable id on last item → cannot paginate safely")
                break

            if next_cursor == cursor_from_id:
                print("  ⚠️  fromId unchanged → cursor semantics unclear")
                break

            cursor_from_id = next_cursor

            if len(items) < args.limit:
                print("  → last page (len < limit), stop")
                break

            time.sleep(args.sleep_between)

    finally:
        session.close()

    # تحلیل
    analysis = analyze_records(all_items, all_ids_across_pages)

    # --- تعیین وضعیت pagination ---
    # pagination_verified = True فقط اگر:
    #   - بیش از ۱ صفحه موفق
    #   - هیچ ID تکراری در سراسر صفحات
    #   - ترتیب زمانی منبع سازگار (asc یا desc)
    if (
        len(page_records) >= 2
        and endpoint_ok
        and not analysis["duplicate_ids"]
        and (analysis["source_order_is_ascending"]
             or analysis["source_order_is_descending"])
    ):
        pagination_verified = True

    # --- تعیین وضعیت data quality ---
    # data_quality_ok = True فقط اگر:
    #   - endpoint_ok
    #   - هیچ رکورد نامعتبر
    #   - همه‌ی delta ها یکسان (یک unique delta)
    #   - نرخ‌ها معتبر
    if endpoint_ok:
        inv = analysis["invalid_records"]
        no_invalid = (
            inv["invalid_timestamps"] == 0
            and inv["invalid_rates"] == 0
            and inv["missing_fields"] == 0
        )
        single_delta = len(analysis["delta_stats"]["unique_deltas_hours"]) <= 1
        data_quality_ok = bool(no_invalid and single_delta)
    else:
        data_quality_ok = None

    # --- چاپ خلاصه ---
    print()
    print("=" * 72)
    print("📊 ANALYSIS")
    print("=" * 72)

    print(f"  target_url         : {url}")
    print(f"  actual_response_url: {actual_response_url}")
    print(f"  endpoint_ok        : {endpoint_ok}")
    print(f"  pagination_verified: {pagination_verified}")
    print(f"  data_quality_ok    : {data_quality_ok}")
    print()

    print(f"  pages_fetched      : {page}")
    print(f"  total_items        : {analysis['total_items']}")
    print(f"  unique_settle_times: {analysis['unique_settle_times']}")
    print(f"  unique_ids         : {analysis['unique_id_count']} "
          f"/ {analysis['total_id_count']}")

    if analysis["oldest_settle_utc"]:
        print(f"  oldest_settle_utc  : {analysis['oldest_settle_utc']}")
        print(f"  newest_settle_utc  : {analysis['newest_settle_utc']}")

    if analysis["source_order_is_ascending"] is not None:
        print(f"  source_order_asc   : {analysis['source_order_is_ascending']}")
        print(f"  source_order_desc  : {analysis['source_order_is_descending']}")

    ds = analysis["delta_stats"]
    if ds["unique_deltas_hours"]:
        print(f"  unique deltas (h)  : {ds['unique_deltas_hours']}")
        print(f"  modal delta (h)    : {ds['modal_delta_hours']}")
        print(f"  delta frequency    : {ds['delta_frequency']}")
        if ds["unexpected_deltas"]:
            print(f"  ⚠️  unexpected     : {ds['unexpected_deltas']}")

    rs = analysis["rate_stats"]
    if rs:
        print(f"  rate count         : {rs['count']}")
        print(f"  rate min/max       : {rs['min']} / {rs['max']}")
        print(f"  rate mean          : {rs['mean']:.8f}")
        print(f"  positive/negative  : {rs['positive_count']} / "
              f"{rs['negative_count']}")

    inv = analysis["invalid_records"]
    if any(inv.values()):
        print(f"  ⚠️  invalid_records: {inv}")

    if analysis["duplicate_ids"]:
        print(f"  ⚠️  duplicate_ids  : {analysis['duplicate_ids']}")

    if errors:
        print()
        print("  Errors:")
        for e in errors:
            print(f"    page {e['page']}: {e['error']}")

    print()
    print("  Sample (first raw item):")
    print(json.dumps(
        all_items[0] if all_items else None,
        indent=2, default=str,
    ))

    print("=" * 72)

    # --- ذخیره‌ی گزارش ---
    exit_code = 0 if endpoint_ok else 1

    if args.out:
        try:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)

            report = {
                "probe_version": "v1.1",
                "target_url": url,
                "actual_response_url": actual_response_url,
                "symbol": args.symbol,
                "limit_per_page": args.limit,
                "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
                # سه وضعیت جداگانه
                "endpoint_ok": endpoint_ok,
                "pagination_verified": pagination_verified,
                "data_quality_ok": data_quality_ok,
                # تحلیل
                "pages_fetched": page,
                "analysis": analysis,
                # داده‌ی خام برای بازبینی
                "page_records": page_records,
                "errors": errors,
            }

            path.write_text(
                json.dumps(report, ensure_ascii=False,
                           indent=2, default=str),
                encoding="utf-8",
            )
            print(f"📝 report written to {args.out}")
        except Exception as e:
            print(f"❌ failed to write report: "
                  f"{type(e).__name__}: {e}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())