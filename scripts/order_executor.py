import math
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass

VALID_SIDES = ("LONG", "SHORT")
VALID_ORDER_TYPES = ("MARKET", "LIMIT")
VALID_INTENTS = ("ENTRY", "EXIT", "TP1_HIT")

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
    if isinstance(v, bool): return False
    if not isinstance(v, (int, float)): return False
    return math.isfinite(v)

def _is_finite_int(v) -> bool:
    if isinstance(v, bool): return False
    return isinstance(v, int)

def _floor_to_step(value: float, step: float) -> float:
    if step <= 0: raise ValueError("step must be > 0")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    n = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(n * d_step)

def _is_multiple_of(value: float, step: float, tol: float = 1e-9) -> bool:
    if step <= 0: return True
    rounded = _floor_to_step(value, step)
    return abs(rounded - value) <= tol

def _reject(gate: str, reason: str) -> Decision:
    return Decision(accepted=False, reason=reason, gate=gate, order=None)

def _validate_specs(specs: dict) -> Decision | None:
    if not isinstance(specs, dict): return _reject("specs", "specs not dict")
    for key in ("min_qty", "max_qty", "qty_step", "contract_multiplier", "tick_size", "max_leverage"):
        v = specs.get(key)
        if v is None: return _reject("specs", f"specs.{key} is None")
        if isinstance(v, bool) or not isinstance(v, (int, float)): return _reject("specs", f"specs.{key} not numeric")
        if not math.isfinite(v) or v <= 0: return _reject("specs", f"specs.{key} invalid value {v}")
    mn = specs.get("min_notional", 0)
    if mn is not None:
        if isinstance(mn, bool) or not isinstance(mn, (int, float)): return _reject("specs", "min_notional not numeric")
        if not math.isfinite(mn) or mn < 0: return _reject("specs", "min_notional negative")
    return None

def _validate_shape(req: OrderRequest) -> Decision | None:
    if not isinstance(req.symbol, str) or not req.symbol.strip(): return _reject("shape.symbol", "empty")
    if req.side not in VALID_SIDES: return _reject("shape.side", f"bad {req.side}")
    if req.order_type not in VALID_ORDER_TYPES: return _reject("shape.order_type", f"bad {req.order_type}")
    if req.intent not in VALID_INTENTS: return _reject("shape.intent", f"bad {req.intent}")
    return None

def _validate_qty(req: OrderRequest, specs: dict) -> Decision | None:
    if not _is_finite_number(req.qty) or req.qty <= 0: return _reject("qty", f"invalid {req.qty}")
    if req.qty < specs["min_qty"]: return _reject("qty", "< min_qty")
    if req.qty > specs["max_qty"]: return _reject("qty", "> max_qty")
    if not _is_multiple_of(req.qty, specs["qty_step"]): return _reject("qty", "not multiple of step")
    return None

def _validate_price(req: OrderRequest, specs: dict) -> Decision | None:
    if req.order_type == "MARKET":
        if req.price is not None: return _reject("price", "MARKET must not have price")
        return None
    if req.price is None or not _is_finite_number(req.price) or req.price <= 0: return _reject("price", "bad LIMIT price")
    if not _is_multiple_of(req.price, specs["tick_size"]): return _reject("price", "not multiple of tick")
    return None

def _validate_leverage(req: OrderRequest, specs: dict, config: dict) -> Decision | None:
    if not _is_finite_int(req.leverage) or req.leverage <= 0: return _reject("leverage", "must be positive int")
    if req.leverage > config.get("leverage_cap", float("inf")): return _reject("leverage", "> cap")
    if req.leverage > specs["max_leverage"]: return _reject("leverage", "> exchange max")
    return None

def _validate_notional(req: OrderRequest, specs: dict, ref_price: float) -> Decision | None:
    min_notional = specs.get("min_notional", 0.0)
    if min_notional <= 0: return None
    price = req.price if req.price is not None else ref_price
    if not _is_finite_number(price) or price <= 0: return _reject("notional", "bad ref_price")
    notional = req.qty * specs["contract_multiplier"] * price
    if notional < min_notional: return _reject("notional", f"{notional} < {min_notional}")
    return None

def _validate_kill_switch(req: OrderRequest, state: dict) -> Decision | None:
    if req.intent in ("EXIT", "TP1_HIT"): return None
    if state.get("circuit_breaker_active", False): return _reject("kill_switch", "circuit breaker")
    if state.get("kill_switch", False): return _reject("kill_switch", "kill switch")
    if state.get("halt_trading", False): return _reject("halt_trading", "halted")
    return None

def _validate_exit_qty_and_side(req: OrderRequest, state: dict) -> Decision | None:
    if req.intent not in ("EXIT", "TP1_HIT"): return None
    pos = state.get("position")
    if pos is None: return _reject("exit", "no position")
    if pos.get("side") != req.side: return _reject("exit", "side mismatch")
    remaining = pos.get("remaining_size", 0.0)
    if not _is_finite_number(remaining) or remaining <= 0: return _reject("exit", "bad remaining_size")
    if req.qty > remaining + 1e-9: return _reject("exit", "> remaining")
    return None

def _validate_no_duplicate(req: OrderRequest, state: dict) -> Decision | None:
    if req.intent != "ENTRY": return None
    if state.get("position") is not None: return _reject("duplicate", "position open")
    if state.get("pending_entry") is not None: return _reject("duplicate", "pending entry")
    if state.get("pending_order_id") is not None: return _reject("duplicate", "pending order")
    return None

def _validate_position_limit(req: OrderRequest, state: dict, config: dict) -> Decision | None:
    if req.intent != "ENTRY": return None
    if state.get("position") is not None and config.get("max_concurrent_positions", 1) <= 1:
        return _reject("position_limit", "limit reached")
    return None

DEFAULT_CONFIG = {"max_concurrent_positions": 1, "leverage_cap": 3}

def evaluate(req: OrderRequest, specs: dict, state: dict, config: dict = None, ref_price: float = None) -> Decision:
    if config is None: config = DEFAULT_CONFIG
    if ref_price is None: ref_price = 0.0
    validators = [
        lambda: _validate_specs(specs), lambda: _validate_shape(req),
        lambda: _validate_qty(req, specs), lambda: _validate_price(req, specs),
        lambda: _validate_leverage(req, specs, config), lambda: _validate_notional(req, specs, ref_price),
        lambda: _validate_kill_switch(req, state), lambda: _validate_exit_qty_and_side(req, state),
        lambda: _validate_no_duplicate(req, state), lambda: _validate_position_limit(req, state, config),
    ]
    for v in validators:
        d = v()
        if d is not None: return d
    return Decision(accepted=True, reason=None, gate=None, order={"symbol": req.symbol, "side": req.side, "order_type": req.order_type, "qty": req.qty, "price": req.price, "leverage": req.leverage, "intent": req.intent})

# TEST HELPER (not used in production path)
class MockAdapter:
    def __init__(self, ref_price: float = 0.0):
        self.ref_price = ref_price
        self.calls = []
    def submit(self, req: OrderRequest, specs: dict, state: dict, config: dict = None) -> Decision:
        decision = evaluate(req, specs, state, config, ref_price=self.ref_price)
        self.calls.append({"request": req.__dict__, "decision": decision.__dict__})
        return decision
    def history(self) -> list: return [dict(c) for c in self.calls]
    def reset(self) -> None: self.calls = []