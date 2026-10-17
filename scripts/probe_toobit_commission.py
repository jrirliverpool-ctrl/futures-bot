"""
probe_toobit_commission.py — دریافت نرخ کارمزد واقعی از Toobit.

هدف (محدود و صریح):
  - تأیید endpoint GET /api/v1/futures/commissionRate
  - دریافت ۴ فیلد کارمزد: openMakerFee, openTakerFee,
    closeMakerFee, closeTakerFee
  - محاسبه‌ی round-trip taker fee (برای Paper با market order)
  - ثبت پاسخ خام برای بازبینی

آنچه این probe اثبات نمی‌کند:
  - نرخ واقعی برای حجم معاملات بزرگ‌تر (VIP tier)
  - نرخ واقعی برای Maker orders (چون Paper فقط Taker دارد)
  - نرخ funding (این جدا بررسی شد)

هیچ سفارشی ارسال نمی‌شود. هیچ تغییری در state، events،
یا کد تولیدی ایجاد نمی‌شود.
فقط GET به endpoint خصوصی با همان API Key که برای Balance
و Positions استفاده شد.

⚠️ توجه درباره‌ی مجوز کلید:
این probe از همان TOOBIT_API_KEY پروژه استفاده می‌کند که
طبق بررسی‌های قبلی مجوز Read + Trade دارد (Withdrawals غیرفعال).
برای اصل حداقل دسترسی (least-privilege)، بهتر است کلید
اختصاصی Read-only استفاده شود. اگر موجود نیست، پیش از اجرا
مجوزهای کلید فعلی را در داشبورد Toobit آگاهانه بررسی کنید.
پرچم `api_key_is_dedicated_readonly` در گزارش ثبت می‌شود.

الزامات محیط:
  - TOOBIT_API_KEY و TOOBIT_API_SECRET در GitHub Secrets

اگر HTTP خطا داد یا فیلدها کامل/معتبر نبودند → exit 1.
"""

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


BASE_URL = "https://api.toobit.com"
COMMISSION_PATH = "/api/v1/futures/commissionRate"
BALANCE_PATH = "/api/v1/futures/balance"

DEFAULT_SYMBOL = "BTC-SWAP-USDT"
DEFAULT_RECV_WINDOW = 5000
DEFAULT_TIMEOUT = 15

REQUIRED_FEE_FIELDS = (
    "openMakerFee",
    "openTakerFee",
    "closeMakerFee",
    "closeTakerFee",
)

# سقف معقول برای نرخ کارمزد — ۱٪ = 0.01
# اگر مقدار بزرگ‌تر بود، احتمالاً واحد (scale) اشتباه است (مثلاً basis points)
MAX_PLAUSIBLE_FEE_RATE = 0.01


# ============================================================
# Sanitization — critical for security
# ============================================================

def _sanitize_url(url: str) -> str:
    """
    حذف signature از URL قبل از ثبت/چاپ.
    API key در هدر است، ولی signature در URL — باید redact شود.
    """
    if "signature=" not in url:
        return url
    parts = url.split("&signature=")
    return parts[0] + "&signature=REDACTED"


def _safe_error_from_exception(e: Exception) -> str:
    """
    متن خطای امن از exception.

    ⚠️ برای requests exceptions، هرگز str(e) را برنگردان —
    ممکن است URL امضاشده را در متن داشته باشد.
    فقط نوع exception ثبت می‌شود.
    """
    if isinstance(e, (
        requests.exceptions.RequestException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.HTTPError,
        requests.exceptions.TooManyRedirects,
        requests.exceptions.URLRequired,
    )):
        return type(e).__name__
    # برای exceptions غیر-requests (signing، JSON، ...) — type name کافی است
    return f"{type(e).__name__}"


# ============================================================
# Auth
# ============================================================

def _sign(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _private_get(api_key: str, api_secret: str,
                 endpoint: str, params: dict = None,
                 recv_window: int = DEFAULT_RECV_WINDOW,
                 timeout: int = DEFAULT_TIMEOUT):
    """
    GET امضاشده به endpoint خصوصی Toobit.

    Returns: dict با فیلدهای:
      http_status      : int یا None
      raw_request_url  : URL با signature=REDACTED
      response_json    : dict یا None
      error            : str یا None (هرگز شامل signature)
    """
    result = {
        "http_status": None,
        "raw_request_url": None,
        "response_json": None,
        "error": None,
    }

    if params is None:
        params = {}

    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = recv_window

    sorted_params = sorted(params.items())
    query_string = "&".join(f"{k}={v}" for k, v in sorted_params)

    try:
        signature = _sign(api_secret, query_string)
    except Exception as e:
        result["error"] = f"signing_failed: {_safe_error_from_exception(e)}"
        return result

    headers = {"X-BB-APIKEY": api_key}
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    result["raw_request_url"] = _sanitize_url(url)

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        # ⚠️ فقط type name — str(e) ممکن است URL داشته باشد
        result["error"] = _safe_error_from_exception(e)
        return result

    result["http_status"] = r.status_code

    try:
        result["response_json"] = r.json()
    except ValueError as e:
        result["error"] = f"json_parse_failed: {_safe_error_from_exception(e)}"
        return result

    if r.status_code >= 400:
        result["error"] = f"HTTP {r.status_code}"
        return result

    return result


# ============================================================
# Semantic error detection
# ============================================================

def _check_logical_error(response_json):
    """
    بعضی APIها 200 برمی‌گردانند ولی در JSON، code/msg/error دارند.

    Returns: (has_error: bool, desc: str یا None)
    """
    if not isinstance(response_json, dict):
        return True, f"response not dict: {type(response_json).__name__}"

    # code: 0 یا 200 = موفق
    code = response_json.get("code")
    if code is not None:
        try:
            code_int = int(code)
        except (ValueError, TypeError):
            return True, f"unparseable code: {code!r}"
        if code_int not in (0, 200):
            msg = (
                response_json.get("msg")
                or response_json.get("message")
                or ""
            )
            return True, f"logical_error code={code}, msg={msg}"

    # success: false → خطا
    if response_json.get("success") is False:
        msg = (
            response_json.get("msg")
            or response_json.get("message")
            or ""
        )
        return True, f"success=false, msg={msg}"

    # error: non-empty → خطا
    err = response_json.get("error")
    if err:
        return True, f"error={err}"

    return False, None


# ============================================================
# Strict numeric validation
# ============================================================

def _parse_fee_strict(value):
    """
    Parse fee as non-negative finite float.

    Returns: float یا None اگر:
      - None / bool
      - غیرعددی
      - NaN یا Infinity
      - منفی
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(v):
        return None
    if v < 0:
        return None
    return v


def _extract_fees(response_json):
    """
    استخراج ۴ فیلد کارمزد از پاسخ.

    Toobit ممکن است پاسخ را به یکی از این اشکال برگرداند:
      {"openMakerFee": "0.0002", ...}
      {"data": {"openMakerFee": ...}}
      {"code": 0, "msg": "", "data": {"openMakerFee": ...}}

    Returns: (found_raw: dict, missing_fields: list)
    """
    candidates = [response_json]
    if isinstance(response_json, dict) and "data" in response_json:
        candidates.append(response_json["data"])

    found = {}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        for field in REQUIRED_FEE_FIELDS:
            if field in c and field not in found:
                found[field] = c[field]

    missing = [f for f in REQUIRED_FEE_FIELDS if f not in found]
    return found, missing


def _validate_fees(found_raw):
    """
    Strict numeric validation.

    Returns: (validated_fees: dict, invalid_fields: list)
    """
    validated = {}
    invalid = []
    for field in REQUIRED_FEE_FIELDS:
        if field not in found_raw:
            continue
        parsed = _parse_fee_strict(found_raw[field])
        if parsed is None:
            invalid.append(field)
        else:
            validated[field] = parsed
    return validated, invalid


def _compute_round_trip_taker(validated_fees):
    """
    round-trip taker fee = openTakerFee + closeTakerFee.
    این یک نسبت (fraction) است، نه مقدار USD.
    مقدار USD = notional × این نسبت.
    """
    open_taker = validated_fees.get("openTakerFee")
    close_taker = validated_fees.get("closeTakerFee")
    if open_taker is None or close_taker is None:
        return None
    return open_taker + close_taker


def _sanity_checks(validated_fees):
    """
    بررسی‌های sanity روی مقادیر معتبرشده.

    Returns: dict of checks (هر value True/False/None).
    """
    checks = {}

    om = validated_fees.get("openMakerFee")
    ot = validated_fees.get("openTakerFee")
    cm = validated_fees.get("closeMakerFee")
    ct = validated_fees.get("closeTakerFee")

    if om is not None and ot is not None:
        checks["open_taker_ge_maker"] = ot >= om
    if cm is not None and ct is not None:
        checks["close_taker_ge_maker"] = ct >= cm

    # همه در بازه‌ی [0, MAX_PLAUSIBLE_FEE_RATE)
    all_plausible = True
    for v in (om, ot, cm, ct):
        if v is None:
            continue
        if v >= MAX_PLAUSIBLE_FEE_RATE:
            all_plausible = False
            break
    checks["all_fees_plausible_lt_1pct"] = all_plausible

    return checks


# ============================================================
# Main
# ============================================================

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Probe Toobit commission rate.",
    )
    p.add_argument("--symbol", default=DEFAULT_SYMBOL)
    p.add_argument("--recv-window", type=int, default=DEFAULT_RECV_WINDOW)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--out", default=None)
    p.add_argument(
        "--api-key-is-dedicated-readonly", action="store_true",
        help="اگر کلید اختصاصی Read-only استفاده می‌شود، این را بگذارید. "
             "در گزارش ثبت می‌شود.",
    )
    args = p.parse_args(argv)

    api_key = os.environ.get("TOOBIT_API_KEY")
    api_secret = os.environ.get("TOOBIT_API_SECRET")

    if not api_key or not api_secret:
        print("❌ TOOBIT_API_KEY or TOOBIT_API_SECRET missing")
        return 1

    print("=" * 72)
    print("🔍 PROBE TOOBIT COMMISSION")
    print("=" * 72)
    print(f"  endpoint    : {BASE_URL}{COMMISSION_PATH}")
    print(f"  symbol      : {args.symbol}")
    print(f"  recv_window : {args.recv_window}")
    print(f"  timeout     : {args.timeout}s")
    print(f"  api_key_mode: "
          f"{'dedicated read-only' if args.api_key_is_dedicated_readonly else 'project default (Read+Trade)'}")
    print("=" * 72)
    print()

    # --- Pre-flight: تأیید auth با balance ---
    print("[Pre-flight] Checking auth with /futures/balance...")
    preflight = _private_get(
        api_key, api_secret, BALANCE_PATH,
        recv_window=args.recv_window, timeout=args.timeout,
    )
    preflight_http_ok = (
        preflight["error"] is None
        and preflight["http_status"] == 200
    )
    preflight_logical_error = None
    if preflight_http_ok:
        has_err, desc = _check_logical_error(preflight["response_json"])
        if has_err:
            preflight_logical_error = desc
    preflight_ok = preflight_http_ok and preflight_logical_error is None

    print(f"  http_status : {preflight['http_status']}")
    print(f"  error       : {preflight['error']}")
    if preflight_logical_error:
        print(f"  logical_err : {preflight_logical_error}")
    print(f"  preflight_ok: {preflight_ok}")
    if not preflight_ok:
        print()
        print("  ❌ Auth preflight failed — aborting before commission call")
        print("     (این یعنی کلید/secret/header یا دسترسی مشکل دارد،")
        print("      نه endpoint کارمزد)")
        return 1
    print()

    # --- Commission call ---
    print("[Main] Calling /futures/commissionRate...")
    rec = _private_get(
        api_key, api_secret, COMMISSION_PATH,
        params={"symbol": args.symbol},
        recv_window=args.recv_window, timeout=args.timeout,
    )

    endpoint_http_ok = (
        rec["error"] is None
        and rec["http_status"] == 200
        and isinstance(rec["response_json"], dict)
    )

    logical_error = None
    if endpoint_http_ok:
        has_err, desc = _check_logical_error(rec["response_json"])
        if has_err:
            logical_error = desc

    endpoint_ok = endpoint_http_ok and logical_error is None

    print(f"  http_status : {rec['http_status']}")
    print(f"  error       : {rec['error']}")
    if logical_error:
        print(f"  logical_err : {logical_error}")
    print(f"  endpoint_ok : {endpoint_ok}")

    fees_found_raw, fees_missing = {}, list(REQUIRED_FEE_FIELDS)
    fees_validated, fees_invalid = {}, []
    round_trip_taker = None
    checks = {}

    if endpoint_ok:
        fees_found_raw, fees_missing = _extract_fees(rec["response_json"])
        fees_validated, fees_invalid = _validate_fees(fees_found_raw)
        round_trip_taker = _compute_round_trip_taker(fees_validated)
        checks = _sanity_checks(fees_validated)

    fields_complete = (
        len(fees_missing) == 0
        and len(fees_invalid) == 0
        and len(fees_validated) == len(REQUIRED_FEE_FIELDS)
    )

    print()
    print("📊 Fee fields:")
    for f in REQUIRED_FEE_FIELDS:
        raw = fees_found_raw.get(f)
        val = fees_validated.get(f)
        if val is not None:
            print(f"  ✅ {f:20s}: {val:.8f}  (raw: {raw!r})")
        elif raw is not None:
            print(f"  ❌ {f:20s}: INVALID  (raw: {raw!r})")
        else:
            print(f"  ❌ {f:20s}: MISSING")

    if fees_missing:
        print(f"  ⚠️  missing fields: {fees_missing}")
    if fees_invalid:
        print(f"  ⚠️  invalid fields: {fees_invalid}")

    if round_trip_taker is not None:
        print()
        print(f"  round-trip taker rate : {round_trip_taker:.8f} "
              f"({round_trip_taker * 100:.5f}%)")
        print(f"    note: rate on notional, not fixed USD")

    if checks:
        print()
        print("  Sanity checks:")
        for k, v in checks.items():
            print(f"    {k:30s}: {v}")

    print()
    print("  Raw response:")
    print(json.dumps(
        rec["response_json"], indent=2, default=str,
    ))

    print("=" * 72)

    # --- Report ---
    exit_code = 0 if (endpoint_ok and fields_complete) else 1

    if args.out:
        try:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)

            report = {
                "probe_version": "v1.1",
                "endpoint": f"{BASE_URL}{COMMISSION_PATH}",
                "raw_request_url": rec["raw_request_url"],
                "symbol": args.symbol,
                "recv_window": args.recv_window,
                "timeout": args.timeout,
                "fetched_at_iso": datetime.now(timezone.utc).isoformat(),

                # Auth context
                "api_key_is_dedicated_readonly":
                    args.api_key_is_dedicated_readonly,
                "api_key_permissions_note": (
                    "The existing project key has Read + Trade permissions "
                    "(withdrawals disabled). For least-privilege, a dedicated "
                    "Read-only key is preferred. Verify permissions in Toobit "
                    "dashboard before relying on this probe's isolation."
                ),

                # Response status
                "http_status": rec["http_status"],
                "endpoint_ok": endpoint_ok,
                "logical_error": logical_error,
                "fields_complete": fields_complete,
                "missing_fields": fees_missing,
                "invalid_fields": fees_invalid,

                # Pre-flight context
                "preflight_ok": preflight_ok,
                "preflight_http_status": preflight["http_status"],
                "preflight_logical_error": preflight_logical_error,

                # Fee data
                "fees_raw": fees_found_raw,
                "fees_validated": fees_validated,
                "round_trip_taker_rate": round_trip_taker,
                "round_trip_taker_rate_pct": (
                    round_trip_taker * 100
                    if round_trip_taker is not None else None
                ),
                "fee_basis_note": (
                    "All fee fields are rates (fraction of notional). "
                    "Actual USD cost = notional × rate. "
                    "For a full round-trip with equal open/close notional, "
                    "total fee fraction = openTakerFee + closeTakerFee. "
                    "Not to be confused with a fixed USD amount."
                ),

                # Sanity
                "sanity_checks": checks,

                # Raw
                "raw_response": rec["response_json"],
                "error": rec["error"],
            }

            path.write_text(
                json.dumps(report, ensure_ascii=False,
                           indent=2, default=str),
                encoding="utf-8",
            )
            print(f"📝 report written to {args.out}")
        except Exception as e:
            print(f"❌ failed to write report: "
                  f"{_safe_error_from_exception(e)}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())