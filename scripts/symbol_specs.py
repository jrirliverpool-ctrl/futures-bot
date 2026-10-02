"""
symbol_specs.py — دریافت و اعتبارسنجی مشخصات قرارداد از Toobit.

مبنا: /api/v1/exchangeInfo → data["contracts"]

نکته: تمام فیلدها از ساختار واقعی Toobit استخراج شده‌اند.
"""

import requests
from datetime import datetime, timezone

BASE_URL = "https://api.toobit.com"
EXCHANGE_INFO_PATH = "/api/v1/exchangeInfo"


def fetch_exchange_info(timeout: int = 15) -> dict:
    """دریافت کل exchangeInfo از Toobit."""
    try:
        r = requests.get(f"{BASE_URL}{EXCHANGE_INFO_PATH}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"exchangeInfo fetch failed: {e}")


def _extract_filter(contract: dict, filter_type: str) -> dict:
    """استخراج یک filter خاص از آرایه filters."""
    for f in contract.get("filters", []):
        if f.get("filterType") == filter_type:
            return f
    return {}


def parse_symbol_specs(contract: dict) -> dict:
    """تبدیل ساختار خام Toobit به dict استاندارد ما."""
    lot_size = _extract_filter(contract, "LOT_SIZE")
    price_filter = _extract_filter(contract, "PRICE_FILTER")
    min_notional_filter = _extract_filter(contract, "MIN_NOTIONAL")

    risk_limits = contract.get("riskLimits", [])
    max_leverage_tier1 = float(risk_limits[0]["maxLeverage"]) if risk_limits else None

    return {
        "symbol": contract["symbol"],
        "status": contract["status"],
        "base_asset": contract.get("baseAsset"),
        "quote_asset": contract.get("quoteAsset"),
        "margin_token": contract.get("marginToken"),

        # Position sizing
        "contract_multiplier": float(contract["contractMultiplier"]),
        "min_qty": float(lot_size.get("minQty", 0)),
        "max_qty": float(lot_size.get("maxQty", 0)),
        "qty_step": float(lot_size.get("stepSize", 0)),
        "min_notional": float(min_notional_filter.get("minNotional", 0)),

        # Price
        "tick_size": float(price_filter.get("tickSize", 0)),
        "min_price": float(price_filter.get("minPrice", 0)),
        "max_price": float(price_filter.get("maxPrice", 0)),

        # Leverage
        "max_leverage": max_leverage_tier1,
        "risk_limits_count": len(risk_limits),

        # متادیتا
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def get_symbol_specs(symbol: str = "BTC-SWAP-USDT") -> dict:
    """دریافت مشخصات یک symbol خاص."""
    info = fetch_exchange_info()
    contracts = info.get("contracts", [])

    for c in contracts:
        if c.get("symbol") == symbol:
            return parse_symbol_specs(c)

    raise ValueError(f"Symbol '{symbol}' not found in contracts.")


def validate_specs(specs: dict) -> tuple[list, list]:
    """اعتبارسنجی مشخصات. (errors, warnings)"""
    errors = []
    warnings = []

    required = [
        "symbol", "status", "contract_multiplier",
        "min_qty", "qty_step", "tick_size",
        "quote_asset", "margin_token",
    ]
    for key in required:
        if key not in specs or specs[key] in (None, ""):
            errors.append(f"❌ فیلد حیاتی خالی: {key}")

    if specs.get("status") != "TRADING":
        errors.append(f"❌ Symbol در وضعیت TRADING نیست: {specs.get('status')}")

    if specs.get("contract_multiplier", 0) <= 0:
        errors.append("❌ contract_multiplier باید > 0 باشد")

    if specs.get("min_qty", 0) <= 0:
        errors.append("❌ min_qty باید > 0 باشد")

    if specs.get("qty_step", 0) <= 0:
        errors.append("❌ qty_step باید > 0 باشد")

    if specs.get("tick_size", 0) <= 0:
        errors.append("❌ tick_size باید > 0 باشد")

    if specs.get("max_leverage") and specs["max_leverage"] < 3:
        warnings.append(f"⚠️ max_leverage ({specs['max_leverage']}) کمتر از هدف ما (3x) است")

    return errors, warnings


def print_specs(specs: dict):
    print("\n" + "=" * 60)
    print(f"📊 SYMBOL SPECS — {specs['symbol']}")
    print("=" * 60)
    for key, val in specs.items():
        if key == "risk_limits_count":
            print(f"  {key}: {val} tier")
        else:
            print(f"  {key}: {val}")
    print("=" * 60)


# --- اجرای مستقیم (تست) ---
if __name__ == "__main__":
    print("🔍 دریافت مشخصات BTC-SWAP-USDT از Toobit...\n")

    try:
        specs = get_symbol_specs("BTC-SWAP-USDT")
    except (RuntimeError, ValueError) as e:
        print(f"❌ خطا: {e}")
        exit(1)

    print_specs(specs)

    errors, warnings = validate_specs(specs)

    print()
    for w in warnings:
        print(w)
    if errors:
        for e in errors:
            print(e)
        print("\n🏁 VALIDATION: FAILED ❌")
        exit(1)

    print("✅ همه فیلدهای حیاتی معتبرند.")
    print("🏁 VALIDATION: PASSED ✅")