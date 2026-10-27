"""Discover and validate Toobit USDT-M swap contracts.

Read-only: this script never places orders.

CRITICAL:
  - Only symbols listed in config/crypto_whitelist.json are accepted.
  - The output payload carries `status`: "complete" | "incomplete".
  - Exit codes: 0=complete, 1=failure, 2=incomplete.
  - If output_path is the canonical repo whitelist (config/symbols_whitelist.json),
    an INCOMPLETE result is NEVER written there. This prevents silently
    replacing a good whitelist with a partial one.

Run from repository root:
    python scripts/probe_all_symbols.py
"""

import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.symbol_specs import (
        fetch_exchange_info,
        parse_symbol_specs,
        validate_specs,
    )
except ImportError:
    from symbol_specs import (
        fetch_exchange_info,
        parse_symbol_specs,
        validate_specs,
    )


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = ROOT / "config" / "symbols_whitelist.json"
CRYPTO_WHITELIST_PATH = ROOT / "config" / "crypto_whitelist.json"

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+-SWAP-USDT$")

REQUIRED_NUMERIC_FIELDS = (
    "contract_multiplier",
    "min_qty",
    "qty_step",
    "tick_size",
)

SOURCE_URL = "https://api.toobit.com/api/v1/exchangeInfo"

EXIT_COMPLETE = 0
EXIT_FAILED = 1
EXIT_INCOMPLETE = 2


def _is_finite_positive(value) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return value > 0


def _is_finite_non_negative(value) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return value >= 0


def load_crypto_whitelist(path: Path) -> set:
    """Load the curated list of candidate symbols.

    Fails closed: missing/malformed file aborts discovery.
    """
    if not path.exists():
        raise RuntimeError(f"Crypto whitelist not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise RuntimeError(f"Crypto whitelist must be a JSON object: {path}")

    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError(f"'symbols' must be a non-empty list in {path}")

    validated = set()
    for sym in symbols:
        if not isinstance(sym, str) or not SYMBOL_PATTERN.fullmatch(sym):
            raise RuntimeError(f"Invalid symbol in crypto whitelist: {sym!r}")
        if sym in validated:
            raise RuntimeError(f"Duplicate symbol in crypto whitelist: {sym!r}")
        validated.add(sym)

    if not validated:
        raise RuntimeError("Crypto whitelist ended up empty after validation")

    return validated


def validate_contract(contract):
    """Return (specs | None, errors, warnings) for one raw contract."""
    errors = []
    warnings = []

    if not isinstance(contract, dict):
        return None, ["Contract is not a JSON object"], warnings

    symbol = contract.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        return None, [f"Unsupported symbol format: {symbol!r}"], warnings

    try:
        specs = parse_symbol_specs(contract)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return None, [f"Cannot parse {symbol}: {exc}"], warnings

    if not isinstance(specs, dict):
        return None, [f"parse_symbol_specs returned non-dict for {symbol}"], warnings

    try:
        validation_result = validate_specs(specs)
    except Exception as exc:
        return None, [f"validate_specs raised for {symbol}: {exc}"], warnings

    if (
        not isinstance(validation_result, tuple)
        or len(validation_result) != 2
        or not isinstance(validation_result[0], list)
        or not isinstance(validation_result[1], list)
    ):
        return None, [
            f"validate_specs returned unexpected shape for {symbol}"
        ], warnings

    errors, warnings = validation_result

    if specs.get("quote_asset") != "USDT":
        errors.append("quote_asset must be USDT")
    if specs.get("margin_token") != "USDT":
        errors.append("margin_token must be USDT")

    for field in REQUIRED_NUMERIC_FIELDS:
        value = specs.get(field)
        if not _is_finite_positive(value):
            errors.append(f"{field} must be a finite positive number")

    min_notional = specs.get("min_notional")
    if not _is_finite_non_negative(min_notional):
        errors.append("min_notional must be a finite non-negative number")

    if specs.get("max_leverage") is None:
        warnings.append("Maximum leverage is unavailable")

    if errors:
        return None, errors, warnings

    return specs, errors, warnings


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).exception(
                    "Could not remove temporary file %s", temp_path
                )


def discover_symbols(crypto_whitelist: set):
    """Return (payload, warnings_count). Raises RuntimeError on fatal failures."""
    info = fetch_exchange_info()

    if not isinstance(info, dict):
        raise RuntimeError("exchangeInfo response is not a JSON object")

    if info.get("status") in ("NETWORK_ERROR", "API_ERROR") or "error" in info:
        raise RuntimeError(f"exchangeInfo API error: {info.get('error')}")

    contracts = info.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise RuntimeError(
            "Missing or empty 'contracts' list; refusing to update whitelist"
        )

    accepted = []
    rejected = []
    excluded_not_allowlisted = []
    found_symbols = set()
    warnings_count = 0
    seen_symbols = set()

    for contract in contracts:
        symbol = contract.get("symbol") if isinstance(contract, dict) else None

        if symbol in seen_symbols:
            rejected.append({
                "symbol": symbol,
                "errors": ["Duplicate symbol in exchangeInfo"],
            })
            continue

        if isinstance(symbol, str):
            seen_symbols.add(symbol)
            found_symbols.add(symbol)

        if symbol not in crypto_whitelist:
            excluded_not_allowlisted.append(symbol if isinstance(symbol, str) else "?")
            continue

        specs, errors, warnings = validate_contract(contract)

        if warnings:
            warnings_count += len(warnings)
            for warning in warnings:
                logging.getLogger(__name__).warning("%s: %s", symbol, warning)

        if errors:
            rejected.append({"symbol": symbol, "errors": errors})
            continue

        accepted.append(specs)

    missing_from_api = sorted(crypto_whitelist - found_symbols)
    accepted.sort(key=lambda item: item["symbol"])
    excluded_not_allowlisted.sort()

    if not accepted:
        raise RuntimeError(
            "No valid USDT-M swap contracts from crypto_whitelist found; "
            "whitelist was not changed"
        )

    is_complete = (
        len(missing_from_api) == 0
        and len(rejected) == 0
        and len(accepted) == len(crypto_whitelist)
    )

    payload = {
        "schema_version": 1,
        "status": "complete" if is_complete else "incomplete",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_URL,
        "mode": "read_only_discovery",
        "filter": "crypto_whitelist",
        "whitelist_size": len(crypto_whitelist),
        "count": len(accepted),
        "symbols": accepted,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "excluded_not_allowlisted_count": len(excluded_not_allowlisted),
        "excluded_not_allowlisted_sample": excluded_not_allowlisted[:50],
        "missing_from_api_count": len(missing_from_api),
        "missing_from_api": missing_from_api,
    }

    return payload, warnings_count


def main(output_path: Path = None) -> int:
    """Return 0=complete, 1=failed, 2=incomplete."""
    if output_path is None:
        output_path = DEFAULT_OUTPUT_PATH

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("ProbeAllSymbols")

    try:
        crypto_whitelist = load_crypto_whitelist(CRYPTO_WHITELIST_PATH)
        logger.info("Crypto whitelist loaded: %d symbols", len(crypto_whitelist))
        payload, warnings_count = discover_symbols(crypto_whitelist)
    except Exception as exc:
        logger.error("Discovery failed; whitelist was not updated: %s", exc)
        return EXIT_FAILED

    is_complete = payload["status"] == "complete"
    output_path_obj = Path(output_path)

    # Never overwrite the canonical repo whitelist with an incomplete result
    is_canonical = output_path_obj.resolve() == DEFAULT_OUTPUT_PATH.resolve()
    if (not is_complete) and is_canonical:
        logger.error(
            "Probe INCOMPLETE; refusing to overwrite canonical whitelist %s. "
            "Run with a different output path (e.g. probe-output/...) to inspect.",
            output_path_obj,
        )
        return EXIT_INCOMPLETE

    atomic_write_json(output_path_obj, payload)

    logger.info("Whitelist saved: %s", output_path_obj)
    logger.info("Status: %s", payload["status"])
    logger.info("Accepted: %d / %d", payload["count"], payload["whitelist_size"])
    logger.info("Rejected: %d", payload["rejected_count"])
    logger.info(
        "Excluded (not in whitelist): %d",
        payload["excluded_not_allowlisted_count"],
    )
    logger.info("Missing from API: %d", payload["missing_from_api_count"])
    logger.info("Warnings: %d", warnings_count)

    if payload["missing_from_api_count"] > 0:
        logger.warning(
            "Symbols in whitelist but missing from API: %s",
            payload["missing_from_api"],
        )
    if payload["rejected_count"] > 0:
        logger.warning(
            "Rejected symbols: %s",
            [r["symbol"] for r in payload["rejected"]],
        )

    for item in payload["symbols"]:
        logger.info(
            "ACCEPT %s | min_qty=%s | qty_step=%s | multiplier=%s",
            item["symbol"],
            item["min_qty"],
            item["qty_step"],
            item["contract_multiplier"],
        )

    return EXIT_COMPLETE if is_complete else EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())