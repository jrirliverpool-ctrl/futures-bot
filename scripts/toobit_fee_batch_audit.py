
"""Read-only Toobit USDT-M commission audit for all configured symbols.

This script performs authenticated GET requests only. It does not place,
amend, or cancel orders; it does not change leverage, margin mode, or state.
A report is complete only when account preflight and every symbol fee
response are valid.
"""
from __future__ import annotations

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
BALANCE_PATH = "/api/v1/futures/balance"
COMMISSION_PATH = "/api/v1/futures/commissionRate"

REQUIRED_FEE_FIELDS = (
    "openMakerFee",
    "openTakerFee",
    "closeMakerFee",
    "closeTakerFee",
)

MAX_PLAUSIBLE_FEE_RATE = 0.01

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHITELIST = ROOT / "config" / "crypto_whitelist.json"


def _private_get(api_key, api_secret, endpoint, params=None, timeout=15):
    values = dict(params or {})
    values["timestamp"] = int(time.time() * 1000)
    values["recvWindow"] = 5000

    query = "&".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
    )

    signature = hmac.new(
        api_secret.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()

    try:
        response = requests.get(
            f"{BASE_URL}{endpoint}?{query}&signature={signature}",
            headers={"X-BB-APIKEY": api_key},
            timeout=timeout,
        )

        try:
            payload = response.json()
        except ValueError:
            return {
                "http_status": response.status_code,
                "response_json": None,
                "error": "json_parse_failed",
            }

        return {
            "http_status": response.status_code,
            "response_json": payload,
            "error": (
                f"HTTP {response.status_code}"
                if response.status_code >= 400
                else None
            ),
        }

    except Exception as exc:
        return {
            "http_status": None,
            "response_json": None,
            "error": type(exc).__name__,
        }


def _check_logical_error(payload):
    if isinstance(payload, list):
        return False, None

    if not isinstance(payload, dict):
        return True, "response_neither_dict_nor_list"

    code = payload.get("code")

    if code is not None:
        try:
            if int(code) not in (0, 200):
                return True, "exchange_code_not_success"
        except (TypeError, ValueError):
            return True, "exchange_code_invalid"

    if payload.get("success") is False or payload.get("error"):
        return True, "exchange_logical_error"

    return False, None


def _extract_fees(payload):
    candidates = [payload]

    if isinstance(payload, dict) and isinstance(
        payload.get("data"), dict
    ):
        candidates.append(payload["data"])

    found = {}

    for candidate in candidates:
        if isinstance(candidate, dict):
            for field in REQUIRED_FEE_FIELDS:
                if field in candidate and field not in found:
                    found[field] = candidate[field]

    missing = [
        field
        for field in REQUIRED_FEE_FIELDS
        if field not in found
    ]

    return found, missing


def _validate_fees(raw):
    validated = {}
    invalid = []

    for field in REQUIRED_FEE_FIELDS:
        if field not in raw:
            continue

        value = raw[field]

        if isinstance(value, bool) or value is None:
            invalid.append(field)
            continue

        try:
            parsed = float(value)
        except (TypeError, ValueError):
            invalid.append(field)
            continue

        if not math.isfinite(parsed) or parsed < 0:
            invalid.append(field)
        else:
            validated[field] = parsed

    return validated, invalid


def _sanity_checks(fees):
    om = fees.get("openMakerFee")
    ot = fees.get("openTakerFee")
    cm = fees.get("closeMakerFee")
    ct = fees.get("closeTakerFee")

    return {
        "open_taker_ge_maker": (
            om is not None and ot is not None and ot >= om
        ),
        "close_taker_ge_maker": (
            cm is not None and ct is not None and ct >= cm
        ),
        "all_fees_plausible_lt_1pct": (
            len(fees) == 4
            and all(
                value < MAX_PLAUSIBLE_FEE_RATE
                for value in fees.values()
            )
        ),
    }


def load_symbols(path: Path = DEFAULT_WHITELIST) -> list[str]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError(
            f"whitelist_read_failed:{type(exc).__name__}"
        ) from None

    symbols = (
        payload.get("symbols")
        if isinstance(payload, dict)
        else None
    )

    if not isinstance(symbols, list) or not symbols:
        raise ValueError("whitelist_symbols_missing")

    if not all(
        isinstance(item, str) and item.strip()
        for item in symbols
    ):
        raise ValueError("whitelist_contains_invalid_symbol")

    if len(symbols) != len(set(symbols)):
        raise ValueError("whitelist_contains_duplicate_symbol")

    return symbols


def _response_ok(record: dict):
    if not isinstance(record, dict):
        return False, "response_record_invalid"

    if record.get("error") is not None:
        return False, "http_or_transport_error"

    if record.get("http_status") != 200:
        return False, "http_status_not_200"

    has_error, description = _check_logical_error(
        record.get("response_json")
    )

    if has_error:
        return False, (
            "exchange_logical_error:"
            + str(description or "unknown")[:120]
        )

    return True, None


def audit_symbol(
    api_key: str,
    api_secret: str,
    symbol: str,
    timeout: int = 15,
) -> dict:

    response = _private_get(
        api_key,
        api_secret,
        COMMISSION_PATH,
        params={"symbol": symbol},
        timeout=timeout,
    )

    ok, error = _response_ok(response)

    if ok:
        raw_fees, missing = _extract_fees(
            response.get("response_json")
        )
    else:
        raw_fees = {}
        missing = list(REQUIRED_FEE_FIELDS)

    fees, invalid = _validate_fees(raw_fees)
    checks = _sanity_checks(fees)

    complete = (
        ok
        and not missing
        and not invalid
        and len(fees) == len(REQUIRED_FEE_FIELDS)
    )

    sanity_ok = (
        checks["open_taker_ge_maker"]
        and checks["close_taker_ge_maker"]
        and checks["all_fees_plausible_lt_1pct"]
    )

    blockers = []

    if error:
        blockers.append(error)

    if missing:
        blockers.append("fee_fields_missing")

    if invalid:
        blockers.append("fee_fields_invalid")

    if complete and not sanity_ok:
        blockers.append("fee_sanity_check_failed")

    return {
        "symbol": symbol,
        "ok": bool(complete and sanity_ok),
        "http_status": response.get("http_status"),
        "fees": fees,
        "missing_fields": missing,
        "invalid_fields": invalid,
        "sanity_checks": checks,
        "round_trip_taker_rate": (
            fees["openTakerFee"] + fees["closeTakerFee"]
            if (
                "openTakerFee" in fees
                and "closeTakerFee" in fees
            )
            else None
        ),
        "blockers": blockers,
    }


def build_report(
    api_key: str,
    api_secret: str,
    symbols: list[str],
    timeout: int = 15,
) -> dict:

    preflight = _private_get(
        api_key,
        api_secret,
        BALANCE_PATH,
        timeout=timeout,
    )

    preflight_ok, preflight_error = _response_ok(preflight)

    if not preflight_ok:
        return {
            "schema": "apexquant_toobit_fee_batch_audit_v1",
            "fetched_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "read_only": True,
            "ok": False,
            "account_preflight_ok": False,
            "account_preflight_blocker": preflight_error,
            "symbols_checked": 0,
            "symbols_passed": 0,
            "symbols_failed": len(symbols),
            "reports": [],
            "orders_submitted": 0,
            "orders_amended": 0,
            "orders_canceled": 0,
            "live_execution_enabled": False,
        }

    reports = [
        audit_symbol(
            api_key,
            api_secret,
            symbol,
            timeout,
        )
        for symbol in symbols
    ]

    passed = sum(
        1 for row in reports
        if row["ok"] is True
    )

    return {
        "schema": "apexquant_toobit_fee_batch_audit_v1",
        "fetched_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "read_only": True,
        "ok": passed == len(symbols),
        "account_preflight_ok": True,
        "account_preflight_blocker": None,
        "symbols_checked": len(reports),
        "symbols_passed": passed,
        "symbols_failed": len(reports) - passed,
        "reports": reports,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "live_execution_enabled": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only batch Toobit commission audit"
    )

    parser.add_argument(
        "--symbols",
        default=None,
        help="Optional comma-separated symbol override",
    )

    parser.add_argument(
        "--whitelist",
        default=str(DEFAULT_WHITELIST),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
    )

    parser.add_argument(
        "--out",
        default="reports/toobit_fee_batch_audit.json",
    )

    args = parser.parse_args(argv)

    if args.timeout < 1 or args.timeout > 60:
        print(
            "Invalid timeout; expected 1..60 seconds",
            file=sys.stderr,
        )
        return 2

    try:
        if args.symbols:
            symbols = [
                part.strip()
                for part in args.symbols.split(",")
                if part.strip()
            ]
        else:
            symbols = load_symbols(Path(args.whitelist))

        if not symbols or len(symbols) != len(set(symbols)):
            raise ValueError("symbol_override_empty_or_duplicate")

    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "read_only": True,
        }, ensure_ascii=False))
        return 2

    api_key = os.environ.get("TOOBIT_API_KEY")
    api_secret = os.environ.get("TOOBIT_API_SECRET")

    if not api_key or not api_secret:
        print(
            "Missing TOOBIT_API_KEY or TOOBIT_API_SECRET",
            file=sys.stderr,
        )
        return 2

    report = build_report(
        api_key,
        api_secret,
        symbols,
        args.timeout,
    )

    output = Path(args.out)
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = {
        "schema": report["schema"],
        "ok": report["ok"],
        "read_only": report["read_only"],
        "account_preflight_ok": report["account_preflight_ok"],
        "symbols_checked": report["symbols_checked"],
        "symbols_passed": report["symbols_passed"],
        "symbols_failed": report["symbols_failed"],
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "live_execution_enabled": False,
        "report_path": str(output),
    }

    print(json.dumps(
        summary,
        ensure_ascii=False,
        indent=2,
    ))

    return 0 if report["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
