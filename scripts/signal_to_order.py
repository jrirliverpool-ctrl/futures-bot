"""
signal_to_order.py — ترجمه سیگنال Strategy به OrderRequest.

پل بین:
    strategy.evaluate_signal()  →  dict {signal, ...}
                                    ↓
    signal_to_order()           →  OrderRequest | None
                                    ↓
    order_executor.evaluate()   →  Decision

قرارداد:
    - NO_TRADE  →  None
    - LONG      →  OrderRequest(side="LONG",  intent="ENTRY")
    - SHORT     →  OrderRequest(side="SHORT", intent="ENTRY")

EXIT:
    - منبع qty = position.remaining_size (نه sizing)
    - منبع symbol = state.symbol (اجباری)
    - منبع leverage = position.leverage یا default

DEFAULT_SIZING فقط برای Dry-Run است.
در Live، sizing واقعی باید از risk layer بیاید.

هیچ I/O، هیچ API. Pure function.
"""

from order_executor import OrderRequest


# ============================================================
# Constants
# ============================================================

VALID_SIGNALS = ("LONG", "SHORT", "NO_TRADE")

# فقط برای Dry-Run. در Live از risk layer استفاده شود.
DEFAULT_SIZING = {
    "qty": 0.001,
    "leverage": 3,
}


# ============================================================
# Validation helpers
# ============================================================

def _validate_sizing(sizing) -> dict:
    """sizing را اعتبارسنجی و نرمال‌سازی می‌کند."""
    if sizing is None:
        return dict(DEFAULT_SIZING)
    if not isinstance(sizing, dict):
        raise ValueError(
            f"sizing must be dict or None, got {type(sizing).__name__}"
        )
    qty = sizing.get("qty")
    leverage = sizing.get("leverage")
    if qty is None or leverage is None:
        raise ValueError("sizing must include 'qty' and 'leverage'")
    return {"qty": qty, "leverage": leverage}


def _resolve_symbol(signal: dict, explicit_symbol) -> str:
    """
    تعیین symbol با اولویت صریح:
      1. explicit_symbol
      2. signal["symbol"]
      3. ValueError
    هرگز fallback بی‌صدا به default.
    """
    candidates = [explicit_symbol, signal.get("symbol")]
    for c in candidates:
        if c is None:
            continue
        if not isinstance(c, str):
            raise ValueError(f"symbol must be str, got {type(c).__name__}")
        if not c.strip():
            raise ValueError("symbol must be non-empty")
        return c
    raise ValueError(
        "symbol not provided — pass explicit_symbol or signal['symbol']"
    )


# ============================================================
# Translator
# ============================================================

def signal_to_order(
    signal: dict,
    sizing: dict = None,
    order_type: str = "MARKET",
    price: float | None = None,
    explicit_symbol: str = None,
) -> OrderRequest | None:
    """
    ترجمه سیگنال strategy به OrderRequest.

    Returns:
        OrderRequest اگر signal در (LONG, SHORT) باشد.
        None اگر NO_TRADE باشد.

    Raises:
        ValueError: signal/sizing/symbol نامعتبر
    """
    if not isinstance(signal, dict):
        raise ValueError(
            f"signal must be dict, got {type(signal).__name__}"
        )

    sig = signal.get("signal")
    if sig not in VALID_SIGNALS:
        raise ValueError(f"invalid signal: {sig!r}")

    if sig == "NO_TRADE":
        return None

    norm_sizing = _validate_sizing(sizing)
    symbol = _resolve_symbol(signal, explicit_symbol)

    return OrderRequest(
        symbol=symbol,
        side=sig,
        order_type=order_type,
        qty=norm_sizing["qty"],
        price=price,
        leverage=norm_sizing["leverage"],
        intent="ENTRY",
    )


def signal_to_exit_order(
    state: dict,
    order_type: str = "MARKET",
    price: float | None = None,
    default_leverage: int = 3,
) -> OrderRequest | None:
    """
    ترجمه‌ی state به OrderRequest برای EXIT.

    قواعد:
      - qty ← pos.remaining_size (اجباری)
      - symbol ← state.symbol یا pos.symbol (اجباری، بدون fallback)
      - leverage ← pos.leverage یا default_leverage

    Returns:
        OrderRequest با intent="EXIT"، یا None اگر پوزیشن باز نیست.

    Raises:
        ValueError: پوزیشن ناسازگار یا symbol پیدا نشد
    """
    if not isinstance(state, dict):
        raise ValueError(
            f"state must be dict, got {type(state).__name__}"
        )

    pos = state.get("position")
    if pos is None:
        return None

    if not isinstance(pos, dict):
        raise ValueError(
            f"position must be dict, got {type(pos).__name__}"
        )

    side = pos.get("side")
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"invalid position side: {side!r}")

    # qty از remaining_size، نه sizing
    qty = pos.get("remaining_size")
    if qty is None:
        raise ValueError("position.remaining_size missing")
    if not isinstance(qty, (int, float)) or qty <= 0:
        raise ValueError(f"invalid position.remaining_size: {qty}")

    # symbol صریح
    symbol = state.get("symbol") or pos.get("symbol")
    if not symbol:
        raise ValueError(
            "symbol not found in state.symbol or position.symbol"
        )
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"invalid symbol: {symbol!r}")

    # leverage
    leverage = pos.get("leverage", default_leverage)
    if not isinstance(leverage, int) or isinstance(leverage, bool):
        raise ValueError(f"invalid leverage: {leverage!r}")
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0: {leverage}")

    return OrderRequest(
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=qty,
        price=price,
        leverage=leverage,
        intent="EXIT",
    )