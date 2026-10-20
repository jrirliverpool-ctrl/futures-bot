"""
order_executor.py — Execution Core (Dry-Run).

⚠️ قرارداد جدید (v1.1):
  - خروج اضطراری (EXIT) حتی با فعال بودن Kill Switch مجاز است.
"""

import math
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass


VALID_SIDES = ("LONG", "SHORT")
VALID_ORDER_TYPES = ("MARKET", "LIMIT")
VALID_INTENTS = ("ENTRY", "EXIT")


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    order_type: str
    qty: float
    price: float | None
    leverage: int
    intent: str


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str | None
    gate: str | None
    order: dict | None


def _is_finite_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


def _is_finite_int(v) -> bool:
    if isinstance(v, bool):
        return False
    return isinstance(v, int)


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step must be > 0")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    n = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(n * d_step)


def _is_multiple_of(value: float, step: float, tol: float = 1e-9) -> bool:
    if step <= 0:
        return True
    rounded = _floor_to_step(value, step)
    return abs(rounded - value) <= tol


def _reject(gate: str, reason: str) -> Decision:
    return Decision(accepted=False, reason=reason, gate=gate, order=None)


def _validate_shape(req: OrderRequest) -> Decision | None:
    if not isinstance(req.symbol, str) or not req.symbol.strip():
        return _reject("shape.symbol", "symbol must be a non-empty string")
    if req.side not in VALID_SIDES:
        return _reject("shape.side", f"invalid side: {req.side!r}")
    if req.order_type not in VALID_ORDER_TYPES:
        return _reject("shape.order_type", f"invalid order_type: {req.order_type!r}")
    if req.intent not in VALID_INTENTS:
        return _reject("shape.intent", f"invalid intent: {req.intent!r}")
    return None


def _validate_qty(req: OrderRequest, specs: dict) -> Decision | None:
    if not _is_finite_number(req.qty):
        return _reject("qty", "qty must be a finite number (not bool/NaN/Inf)")
    min_qty = specs.get("min_qty", 0)
    max_qty = specs.get("max_qty", float("inf"))
    step = specs.get("qty_step", 0)
    if req.qty <= 0:
        return _reject("qty", f"qty must be > 0, got {req.qty}")
    if req.qty < min_qty:
        return _reject("qty", f"qty {req.qty} < min_qty {min_qty}")
    if req.qty > max_qty:
        return _reject("qty", f"qty {req.qty} > max_qty {max_qty}")
    if step > 0 and not _is_multiple_of(req.qty, step):
        return _reject("qty", f"qty {req.qty} not multiple of step {step}")
    return None


def _validate_price(req: OrderRequest, specs: dict) -> Decision | None:
    if req.order_type == "MARKET":
        if req.price is not None:
            return _reject("price", "MARKET order must not specify price")
        return None
    if req.price is None:
        return _reject("price", "LIMIT order requires a price")
    if not _is_finite_number(req.price):
        return _reject("price", "price must be a finite number (not bool/NaN/Inf)")
    if req.price <= 0:
        return _reject("price", f"price must be > 0, got {req.price}")
    tick = specs.get("tick_size", 0)
    if tick > 0 and not _is_multiple_of(req.price, tick):
        return _reject("price", f"price {req.price} not multiple of tick {tick}")
    return None


def _validate_leverage(req: OrderRequest, specs: dict, config: dict) -> Decision | None:
    if not _is_finite_int(req.leverage):
        return _reject("leverage", "leverage must be an int (not bool)")
    if req.leverage <= 0:
        return _reject("leverage", f"leverage must be > 0, got {req.leverage}")
    cap = config.get("leverage_cap", float("inf"))
    if req.leverage > cap:
        return _reject("leverage", f"leverage {req.leverage} > cap {cap}")
    max_lev = specs.get("max_leverage", float("inf"))
    if req.leverage > max_lev:
        return _reject("leverage", f"leverage {req.leverage} > exchange max {max_lev}")
    return None


def _validate_notional(req: OrderRequest, specs: dict, ref_price: float) -> Decision | None:
    min_notional = specs.get("min_notional", 0)
    if min_notional <= 0:
        return None
    mult = specs.get("contract_multiplier", 1.0)
    price = req.price if req.price is not None else ref_price
    if not _is_finite_number(price) or price <= 0:
        return _reject("notional", f"cannot compute notional: ref_price={price}")
    notional = req.qty * mult * price
    if notional < min_notional:
        return _reject("notional", f"notional {notional:.6f} < min {min_notional}")
    return None


# ✅ اصلاح: req را اضافه کردیم تا از intent مطلع شود
def _validate_kill_switch(req: OrderRequest, state: dict) -> Decision | None:
    # قرارداد: خروج اضطراری (EXIT) همیشه مجاز است
    if req.intent == "EXIT":
        return None
    
    if state.get("circuit_breaker_active", False):
        return _reject("kill_switch", "circuit breaker active")
    if state.get("kill_switch", False):
        return _reject("kill_switch", "kill switch explicitly enabled")
    return None


def _validate_no_duplicate(req: OrderRequest, state: dict) -> Decision | None:
    if req.intent != "ENTRY":
        return None
    if state.get("position") is not None:
        return _reject("duplicate", "position already open")
    if state.get("pending_entry") is not None:
        return _reject("duplicate", "pending entry already exists")
    return None


def _validate_position_limit(req: OrderRequest, state: dict, config: dict) -> Decision | None:
    if req.intent != "ENTRY":
        return None
    max_pos = config.get("max_concurrent_positions", 1)
    open_count = 0
    if state.get("position") is not None:
        open_count += 1
    if open_count >= max_pos:
        return _reject("position_limit", f"open positions {open_count} >= limit {max_pos}")
    return None


DEFAULT_CONFIG = {
    "max_concurrent_positions": 1,
    "leverage_cap": 3,
}


def evaluate(req: OrderRequest, specs: dict, state: dict, config: dict = None, ref_price: float = None) -> Decision:
    if config is None:
        config = DEFAULT_CONFIG
    if ref_price is None:
        ref_price = 0.0

    d = _validate_shape(req)
    if d: return d
    d = _validate_qty(req, specs)
    if d: return d
    d = _validate_price(req, specs)
    if d: return d
    d = _validate_leverage(req, specs, config)
    if d: return d
    d = _validate_notional(req, specs, ref_price)
    if d: return d

    # ✅ اصلاح: req را به _validate_kill_switch پاس می‌دهیم
    d = _validate_kill_switch(req, state)
    if d: return d
    
    d = _validate_no_duplicate(req, state)
    if d: return d
    d = _validate_position_limit(req, state, config)
    if d: return d

    return Decision(
        accepted=True, reason=None, gate=None,
        order={
            "symbol": req.symbol, "side": req.side,
            "order_type": req.order_type, "qty": req.qty,
            "price": req.price, "leverage": req.leverage, "intent": req.intent,
        },
    )


class MockAdapter:
    def __init__(self, ref_price: float = 0.0):
        self.ref_price = ref_price
        self.calls: list = []

    def submit(self, req: OrderRequest, specs: dict, state: dict, config: dict = None) -> Decision:
        decision = evaluate(req, specs, state, config, ref_price=self.ref_price)
        self.calls.append({
            "request": {
                "symbol": req.symbol, "side": req.side, "order_type": req.order_type,
                "qty": req.qty, "price": req.price, "leverage": req.leverage, "intent": req.intent,
            },
            "accepted": decision.accepted, "gate": decision.gate, "reason": decision.reason,
        })
        return decision

    def history(self) -> list:
        return [dict(c) for c in self.calls]

    def reset(self) -> None:
        self.calls = []