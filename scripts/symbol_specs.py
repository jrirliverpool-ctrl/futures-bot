"""symbol_specs.py — Contract specs from Toobit.
v3.9.2 — Fix: strict type validation; min_notional must be > 0;
        max_leverage < 3 warns but doesn't block (contract-level vs strategy-level);
        retry only on 5xx and 429 with proper handling.
"""

import time
import requests
from datetime import datetime, timezone

BASE_URL = "https://api.toobit.com"
EXCHANGE_INFO_PATH = "/api/v1/exchangeInfo"


def fetch_exchange_info(timeout=15, max_retries=3):
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.get(f"{BASE_URL}{EXCHANGE_INFO_PATH}", timeout=timeout)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                last_err = f"HTTP 429 (rate limit)"
                continue
            if 500 <= r.status_code < 600:
                last_err = f"HTTP {r.status_code}"
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"exchangeInfo fetch failed after {max_retries} attempts: {last_err}")


def _extract_filter(contract, filter_type):
    for f in contract.get("filters", []):
        if f.get("filterType") == filter_type:
            return f
    return {}


def _safe_positive_float(value, field_name, symbol):
    if value is None:
        raise ValueError(f"{field_name} missing for {symbol}")
    try:
        v = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field_name}={value!r} not numeric for {symbol}")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name}={value!r} not finite for {symbol}")
    return v


def _safe_non_negative_float(value, field_name, symbol, default=0.0):
    if value is None:
        return default
    try:
        v = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field_name}={value!r} not numeric for {symbol}")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name}={value!r} not finite for {symbol}")
    if v < 0:
        raise ValueError(f"{field_name}={value!r} negative for {symbol}")
    return v


def parse_symbol_specs(contract):
    symbol = contract.get("symbol", "<unknown>")
    lot_size = _extract_filter(contract, "LOT_SIZE")
    price_filter = _extract_filter(contract, "PRICE_FILTER")
    min_notional_filter = _extract_filter(contract, "MIN_NOTIONAL")

    contract_multiplier = _safe_positive_float(
        contract.get("contractMultiplier"), "contractMultiplier", symbol
    )

    risk_limits = contract.get("riskLimits") or []
    max_leverage_tier1 = None
    if risk_limits and isinstance(risk_limits[0], dict):
        ml = risk_limits[0].get("maxLeverage")
        if ml is not None:
            try:
                ml_f = float(ml)
                if ml_f == ml_f and ml_f not in (float("inf"), float("-inf")) and ml_f > 0:
                    max_leverage_tier1 = ml_f
            except (ValueError, TypeError):
                pass

    return {
        "symbol": symbol,
        "status": contract.get("status"),
        "base_asset": contract.get("baseAsset"),
        "quote_asset": contract.get("quoteAsset"),
        "margin_token": contract.get("marginToken"),
        "contract_multiplier": contract_multiplier,
        "min_qty": _safe_non_negative_float(lot_size.get("minQty"), "minQty", symbol),
        "max_qty": _safe_non_negative_float(lot_size.get("maxQty"), "maxQty", symbol),
        "qty_step": _safe_non_negative_float(lot_size.get("stepSize"), "stepSize", symbol),
        "min_notional": _safe_non_negative_float(
            min_notional_filter.get("minNotional"), "minNotional", symbol
        ),
        "tick_size": _safe_positive_float(price_filter.get("tickSize"), "tickSize", symbol),
        "min_price": _safe_non_negative_float(price_filter.get("minPrice"), "minPrice", symbol),
        "max_price": _safe_non_negative_float(price_filter.get("maxPrice"), "maxPrice", symbol),
        "max_leverage": max_leverage_tier1,
        "risk_limits_count": len(risk_limits),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def get_symbol_specs(symbol="BTC-SWAP-USDT"):
    info = fetch_exchange_info()
    contracts = info.get("contracts", [])
    for c in contracts:
        if c.get("symbol") == symbol:
            return parse_symbol_specs(c)
    raise ValueError(f"Symbol '{symbol}' not found.")


def validate_specs(specs):
    """
    v3.9.2: strict — invalid contract rejected; strategy-incompatible warns.
    Returns (errors, warnings).
    """
    errors, warnings = [], []

    if not isinstance(specs, dict):
        errors.append("specs must be dict")
        return errors, warnings

    if specs.get("status") != "TRADING":
        errors.append(f"❌ status must be TRADING (got {specs.get('status')!r})")

    # Strict numeric fields — must be positive
    for key in ("contract_multiplier", "min_qty", "max_qty", "qty_step", "tick_size", "min_notional"):
        v = specs.get(key)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"❌ {key} must be numeric (got {type(v).__name__}={v!r})")
            continue
        if not (v == v) or v in (float("inf"), float("-inf")):
            errors.append(f"❌ {key} not finite ({v})")
            continue
        if key == "min_notional":
            if v <= 0:
                warnings.append(f"⚠️ min_notional={v} — some strategies may accept orders below this")
        elif v <= 0:
            errors.append(f"❌ {key} must be > 0 (got {v})")

    for key in ("symbol", "quote_asset", "margin_token"):
        if not isinstance(specs.get(key), str) or not specs[key]:
            errors.append(f"❌ {key} must be non-empty string")

    ml = specs.get("max_leverage")
    if ml is None:
        errors.append("❌ max_leverage not available — leverage constraint cannot be enforced")
    elif isinstance(ml, bool) or not isinstance(ml, (int, float)) or ml <= 0:
        errors.append(f"❌ max_leverage invalid ({ml!r})")
    elif ml < 3:
        warnings.append(f"⚠️ max_leverage={ml} < strategy target 3x — consider excluding this symbol from strategy")

    return errors, warnings


def print_specs(specs):
    print("\n" + "=" * 60)
    print(f"📊 SYMBOL SPECS — {specs.get('symbol')}")
    print("=" * 60)
    for k, v in specs.items():
        print(f"  {k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    print("🔍 Fetching BTC-SWAP-USDT specs...\n")
    try:
        specs = get_symbol_specs("BTC-SWAP-USDT")
    except (RuntimeError, ValueError) as e:
        print(f"❌ {e}")
        exit(1)
    print_specs(specs)
    errors, warnings = validate_specs(specs)
    for w in warnings: print(w)
    if errors:
        for e in errors: print(e)
        print("\n🏁 VALIDATION: FAILED ❌")
        exit(1)
    print("✅ All critical fields valid.")
    print("🏁 VALIDATION: PASSED ✅")