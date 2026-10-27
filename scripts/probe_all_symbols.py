"""Discover and validate Toobit USDT-M swap contracts.

Read-only: this script never places orders.
Existing whitelist is preserved if discovery or validation fails.

Run from repository root:
    python scripts/probe_all_symbols.py

Exit codes:
    0 - whitelist successfully updated
    1 - discovery or validation failed; previous whitelist preserved
"""

import json
import logging
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

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+-SWAP-USDT$")

REQUIRED_NUMERIC_FIELDS = (
    "contract_multiplier",
    "min_qty",
    "qty_step",
    "tick_size",
)

SOURCE_URL = "https://api.toobit.com/api/v1/exchangeInfo"


def validate_contract(contract):
    """Return (specs | None, errors:list, warnings:list) for one raw contract."""

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
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            errors.append(f"{field} must be a positive number")

    min_notional = specs.get("min_notional")
    if isinstance(min_notional, bool) or not isinstance(min_notional, (int, float)):
        errors.append("min_notional must be a number")
    elif min_notional < 0:
        errors.append("min_notional must be non-negative")

    if specs.get("max_leverage") is None:
        warnings.append("Maximum leverage is unavailable")

    return specs, errors, warnings


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically. Existing file is preserved on any failure."""
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


def discover_symbols():
    """Fetch exchange metadata and build the whitelist payload.

    Raises RuntimeError on any failure that should prevent writing a new file.
    """
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

        specs, errors, warnings = validate_contract(contract)

        if warnings:
            warnings_count += len(warnings)
            for warning in warnings:
                logging.getLogger(__name__).warning("%s: %s", symbol, warning)

        if errors:
            rejected.append({
                "symbol": symbol,
                "errors": errors,
            })
            continue

        accepted.append(specs)

    accepted.sort(key=lambda item: item["symbol"])

    if not accepted:
        raise RuntimeError(
            "No valid USDT-M swap contracts found; whitelist was not changed"
        )

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_URL,
        "mode": "read_only_discovery",
        "count": len(accepted),
        "symbols": accepted,
        "rejected_count": len(rejected),
        "rejected": rejected,
    }

    return payload, warnings_count


def main(output_path: Path = None) -> int:
    if output_path is None:
        output_path = DEFAULT_OUTPUT_PATH

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("ProbeAllSymbols")

    try:
        payload, warnings_count = discover_symbols()
        atomic_write_json(output_path, payload)
    except Exception as exc:
        logger.error("Discovery failed; whitelist was not updated: %s", exc)
        return 1

    logger.info("Whitelist saved: %s", output_path)
    logger.info("Accepted: %d", payload["count"])
    logger.info("Rejected: %d", payload["rejected_count"])
    logger.info("Warnings: %d", warnings_count)

    for item in payload["symbols"]:
        logger.info(
            "ACCEPT %s | min_qty=%s | qty_step=%s | multiplier=%s",
            item["symbol"],
            item["min_qty"],
            item["qty_step"],
            item["contract_multiplier"],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())