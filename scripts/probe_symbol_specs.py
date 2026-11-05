
"""Read-only Toobit USDT-M contract specification probe.

Fetches public exchangeInfo and contract ticker prices.
Reports LOT_SIZE token quantities, contract multiplier, estimated
minimum notional and equivalent contract quantity.

No account access. No order placement, amendment or cancellation.
"""

import json
import sys
import time
from decimal import Decimal, InvalidOperation

import requests

BASE_URL = "https://api.toobit.com"
EXCHANGE_INFO_URL = f"{BASE_URL}/api/v1/exchangeInfo"
TICKER_URL = f"{BASE_URL}/quote/v1/contract/ticker/price"

TARGET_SYMBOLS = [
    "BTC-SWAP-USDT",
    "ETH-SWAP-USDT",
    "SOL-SWAP-USDT",
    "BNB-SWAP-USDT",
    "XRP-SWAP-USDT",
    "ADA-SWAP-USDT",
    "DOGE-SWAP-USDT",
    "AVAX-SWAP-USDT",
    "LINK-SWAP-USDT",
    "DOT-SWAP-USDT",
]

TIMEOUT = 20
MAX_RETRIES = 3


def get_json(url):
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=TIMEOUT)

            if response.status_code == 429:
                delay = int(response.headers.get("Retry-After", 2 ** attempt))
                time.sleep(delay)
                last_error = "HTTP 429"
                continue

            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                time.sleep(2 ** attempt)
                continue

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"GET failed: {url}: {last_error}")


def dec(value, field):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}_missing")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field}_not_numeric") from None

    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field}_not_positive_or_non_finite")

    return result


def find_filter(contract, filter_type):
    filters = contract.get("filters")

    if not isinstance(filters, list):
        raise ValueError("filters_missing")

    matches = [
        item for item in filters
        if isinstance(item, dict)
        and item.get("filterType") == filter_type
    ]

    if len(matches) > 1:
        raise ValueError(f"duplicate_filter_{filter_type}")

    return matches[0] if matches else {}


def main():
    print("=" * 78)
    print("TOOBIT USDT-M PUBLIC CONTRACT PROBE — READ ONLY")
    print("=" * 78)

    info = get_json(EXCHANGE_INFO_URL)

    if not isinstance(info, dict):
        raise RuntimeError("exchangeInfo_response_invalid")

    contracts = info.get("contracts")

    if not isinstance(contracts, list):
        raise RuntimeError("exchangeInfo_contracts_missing")

    contract_map = {}

    for contract in contracts:
        if isinstance(contract, dict):
            symbol = contract.get("symbol")
            if isinstance(symbol, str):
                contract_map[symbol] = contract

    print(f"Contracts returned: {len(contract_map)}")

    ticker_data = get_json(TICKER_URL)

    if isinstance(ticker_data, dict):
        # Accept a wrapped response only if its data is an array.
        ticker_data = ticker_data.get("data", ticker_data)

    if not isinstance(ticker_data, list):
        raise RuntimeError("ticker_response_not_array")

    prices = {}

    for row in ticker_data:
        if not isinstance(row, dict):
            continue

        symbol = row.get("s") or row.get("symbol")
        price = row.get("p") or row.get("price")

        if isinstance(symbol, str) and price is not None:
            prices[symbol] = price

    print(f"Ticker prices returned: {len(prices)}")
    print()

    results = []

    for symbol in TARGET_SYMBOLS:
        print("-" * 78)
        print(f"SYMBOL: {symbol}")

        contract = contract_map.get(symbol)

        if contract is None:
            row = {
                "symbol": symbol,
                "ok": False,
                "error": "contract_not_found",
            }
            results.append(row)
            print(json.dumps(row, indent=2))
            continue

        try:
            lot = find_filter(contract, "LOT_SIZE")
            notional_filter = find_filter(contract, "MIN_NOTIONAL")
            trade_amount = find_filter(contract, "TRADE_AMOUNT")

            min_qty = dec(lot.get("minQty"), "LOT_SIZE.minQty")
            step_size = dec(lot.get("stepSize"), "LOT_SIZE.stepSize")
            multiplier = dec(
                contract.get("contractMultiplier"),
                "contractMultiplier",
            )
            price = dec(prices.get(symbol), "ticker_price")

            # Official exchangeInfo documentation describes minQty and
            # stepSize for contracts as token quantities, not contract counts.
            min_notional_estimate = min_qty * price
            min_contract_qty = min_qty / multiplier
            contract_step = step_size / multiplier

            raw_min_notional = notional_filter.get("minNotional")
            raw_min_amount = trade_amount.get("minAmount")

            result = {
                "symbol": symbol,
                "ok": True,
                "status": contract.get("status"),
                "price_source": "public_contract_ticker",
                "last_price": str(price),
                "lot_size_min_qty_token": str(min_qty),
                "lot_size_step_token": str(step_size),
                "contract_multiplier": str(multiplier),
                "estimated_min_notional_usdt": str(min_notional_estimate),
                "equivalent_min_contract_quantity": str(min_contract_qty),
                "equivalent_contract_step": str(contract_step),
                "min_trade_precision": contract.get("minTradePrecision"),
                "raw_MIN_NOTIONAL_minNotional": raw_min_notional,
                "raw_TRADE_AMOUNT_minAmount": raw_min_amount,
                "base_asset": contract.get("baseAsset"),
                "quote_asset": contract.get("quoteAsset"),
                "margin_token": contract.get("marginToken"),
                "inverse": contract.get("inverse"),
                "risk_limits_count": len(contract.get("riskLimits") or []),
            }

            results.append(result)
            print(json.dumps(result, indent=2, ensure_ascii=False))

        except Exception as exc:
            result = {
                "symbol": symbol,
                "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            }
            results.append(result)
            print(json.dumps(result, indent=2, ensure_ascii=False))

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)

    print(json.dumps(results, indent=2, ensure_ascii=False))

    success_count = sum(1 for row in results if row.get("ok") is True)

    print()
    print(f"Symbols checked: {len(TARGET_SYMBOLS)}")
    print(f"Successful: {success_count}")
    print(f"Failed: {len(TARGET_SYMBOLS) - success_count}")
    print("Orders submitted: 0")
    print("Orders amended: 0")
    print("Orders canceled: 0")

    if success_count != len(TARGET_SYMBOLS):
        return 2

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"FATAL: {type(exc).__name__}: {str(exc)[:240]}",
            file=sys.stderr,
        )
        raise SystemExit(1)
