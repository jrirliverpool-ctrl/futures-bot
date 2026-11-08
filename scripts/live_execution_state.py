"""Independent, fail-closed state for real Toobit USDT-M execution.

This module:
- Never sends exchange requests.
- Never reads or imports V1/V2 simulation state.
- Derives positions exclusively from normalized exchange fills.
- Persists state atomically to an explicitly configured absolute path.
- Uses an OS file lock to serialize local writers.
- Fails closed on corruption, identity mismatch or invalid transitions.

IMPORTANT:
An atomic local file is NOT durable across ephemeral GitHub Actions runners.
The caller must provide a genuinely persistent, private storage mount before
using this store for live execution.
"""

import copy
import fcntl
import hashlib
import json
import os
import tempfile
import time
from decimal import Decimal, InvalidOperation


SCHEMA_VERSION = 1
VENUE = "TOOBIT"
MARKET = "USDT-M"
ENVIRONMENTS = {"DEMO", "LIVE"}
OPEN_SIDES = {"BUY_OPEN", "SELL_OPEN"}
CLOSE_SIDES = {"BUY_CLOSE", "SELL_CLOSE"}


class LiveStateError(RuntimeError):
    """Raised when live state is unsafe or invalid."""


def _decimal(value, field, allow_zero=True):
    if isinstance(value, bool) or value is None:
        raise LiveStateError(f"{field} must be numeric")

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LiveStateError(f"{field} must be numeric") from None

    if not number.is_finite():
        raise LiveStateError(f"{field} must be finite")

    if number < 0 or (not allow_zero and number == 0):
        raise LiveStateError(f"{field} is outside allowed range")

    return number


def _required_text(value, field, limit=128):
    if not isinstance(value, str):
        raise LiveStateError(f"{field} must be a string")

    value = value.strip()

    if not value or len(value) > limit:
        raise LiveStateError(f"{field} is invalid")

    return value


def _canonical_fill(fill):
    if not isinstance(fill, dict):
        raise LiveStateError("fill must be an object")

    trade_id = _required_text(fill.get("trade_id"), "trade_id", 128)
    symbol = _required_text(fill.get("symbol"), "symbol", 64)
    side = _required_text(fill.get("side"), "side", 32)

    if side not in OPEN_SIDES | CLOSE_SIDES:
        raise LiveStateError(f"unsupported fill side: {side}")

    timestamp = fill.get("timestamp_ms")

    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
    ):
        raise LiveStateError("invalid fill timestamp_ms")

    qty = _decimal(fill.get("qty"), "fill.qty", allow_zero=False)
    price = _decimal(fill.get("price"), "fill.price", allow_zero=False)

    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "timestamp_ms": timestamp,
        "qty": qty,
        "price": price,
    }


def derive_positions_from_fills(fills):
    """Rebuild one-way positions from a complete, deduplicated fill history.

    The input must contain the complete history for the account and symbols
    being reconstructed. Missing history cannot be detected by this function;
    the caller must prove pagination and coverage separately.
    """

    if not isinstance(fills, list):
        raise LiveStateError("fills must be a list")

    normalized = [_canonical_fill(row) for row in fills]

    seen_ids = set()

    for row in normalized:
        trade_id = row["trade_id"]

        if trade_id in seen_ids:
            raise LiveStateError(f"duplicate trade_id: {trade_id}")

        seen_ids.add(trade_id)

    normalized.sort(
        key=lambda row: (row["timestamp_ms"], row["trade_id"])
    )

    positions = {}

    for fill in normalized:
        symbol = fill["symbol"]
        side = fill["side"]
        qty = fill["qty"]
        price = fill["price"]

        current = positions.get(symbol)

        if side in OPEN_SIDES:
            new_side = "LONG" if side == "BUY_OPEN" else "SHORT"

            if current is None:
                positions[symbol] = {
                    "side": new_side,
                    "quantity": str(qty),
                    "avg_price": str(price),
                }
                continue

            if current["side"] != new_side:
                raise LiveStateError(
                    f"opposite-side open without a closing fill: {symbol}"
                )

            old_qty = Decimal(current["quantity"])
            old_price = Decimal(current["avg_price"])
            total_qty = old_qty + qty
            average = (
                old_qty * old_price + qty * price
            ) / total_qty

            positions[symbol] = {
                "side": new_side,
                "quantity": str(total_qty),
                "avg_price": str(average),
            }
            continue

        if current is None:
            raise LiveStateError(
                f"closing fill has no matching open position: {symbol}"
            )

        expected_close = (
            "SELL_CLOSE"
            if current["side"] == "LONG"
            else "BUY_CLOSE"
        )

        if side != expected_close:
            raise LiveStateError(
                f"closing side mismatch for {symbol}"
            )

        old_qty = Decimal(current["quantity"])

        if qty > old_qty:
            raise LiveStateError(
                f"closing quantity exceeds position for {symbol}"
            )

        remaining = old_qty - qty

        if remaining == 0:
            del positions[symbol]
        else:
            positions[symbol] = {
                "side": current["side"],
                "quantity": str(remaining),
                "avg_price": current["avg_price"],
            }

    return positions


def _state_digest(state):
    payload = {
        key: value
        for key, value in state.items()
        if key != "integrity_sha256"
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def _seal(state):
    result = copy.deepcopy(state)
    result["integrity_sha256"] = _state_digest(result)
    return result


def _validate_state(state, expected_identity=None):
    if not isinstance(state, dict):
        raise LiveStateError("live state must be an object")

    if state.get("schema_version") != SCHEMA_VERSION:
        raise LiveStateError("unsupported live state schema")

    if state.get("venue") != VENUE or state.get("market") != MARKET:
        raise LiveStateError("venue or market identity mismatch")

    if state.get("environment") not in ENVIRONMENTS:
        raise LiveStateError("invalid environment")

    identity = state.get("account_identity")

    if not isinstance(identity, str) or not identity.strip():
        raise LiveStateError("account_identity missing")

    if expected_identity is not None and identity != expected_identity:
        raise LiveStateError("account identity mismatch")

    strategy_hash = state.get("strategy_hash")

    if not isinstance(strategy_hash, str) or not strategy_hash.strip():
        raise LiveStateError("strategy_hash missing")

    sequence = state.get("sequence")

    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
    ):
        raise LiveStateError("invalid state sequence")

    if not isinstance(state.get("halted"), bool):
        raise LiveStateError("halted must be boolean")

    if not isinstance(state.get("halt_reason"), (str, type(None))):
        raise LiveStateError("invalid halt_reason")

    if not isinstance(state.get("positions"), dict):
        raise LiveStateError("positions must be an object")

    if not isinstance(state.get("processed_trade_ids"), list):
        raise LiveStateError("processed_trade_ids must be a list")

    if len(set(state["processed_trade_ids"])) != len(
        state["processed_trade_ids"]
    ):
        raise LiveStateError("duplicate processed trade IDs")

    if not isinstance(state.get("balance_snapshot"), (dict, type(None))):
        raise LiveStateError("invalid balance_snapshot")

    if not isinstance(
        state.get("reconciliation_snapshot"),
        (dict, type(None)),
    ):
        raise LiveStateError("invalid reconciliation_snapshot")

    supplied_digest = state.get("integrity_sha256")

    if (
        not isinstance(supplied_digest, str)
        or supplied_digest != _state_digest(state)
    ):
        raise LiveStateError("state integrity hash mismatch")

    return True


def create_initial_state(
    account_identity,
    environment,
    strategy_hash,
    now_ms=None,
):
    account_identity = _required_text(
        account_identity, "account_identity", 128
    )
    strategy_hash = _required_text(strategy_hash, "strategy_hash", 128)

    if environment not in ENVIRONMENTS:
        raise ValueError("environment must be DEMO or LIVE")

    timestamp = int(time.time() * 1000) if now_ms is None else now_ms

    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("now_ms must be an integer")

    if timestamp < 0:
        raise ValueError("now_ms must be non-negative")

    state = {
        "schema_version": SCHEMA_VERSION,
        "venue": VENUE,
        "market": MARKET,
        "environment": environment,
        "account_identity": account_identity,
        "strategy_hash": strategy_hash,
        "sequence": 0,
        "created_at_ms": timestamp,
        "updated_at_ms": timestamp,
        "halted": True,
        "halt_reason": "initial_state_requires_reconciliation",
        "positions": {},
        "processed_trade_ids": [],
        "balance_snapshot": None,
        "reconciliation_snapshot": None,
        "last_event": "INITIALIZED",
    }

    return _seal(state)


class LiveExecutionStateStore:
    """Atomic local state store; requires a persistent private filesystem."""

    def __init__(self, path, account_identity):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("state path must be an absolute path")

        self.path = path
        self.lock_path = path + ".lock"
        self.account_identity = _required_text(
            account_identity, "account_identity", 128
        )

    def _locked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        return open(self.lock_path, "a+", encoding="utf-8")

    def load(self):
        if not os.path.exists(self.path):
            raise LiveStateError(
                "live state file missing; initialization is required"
            )

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveStateError(
                f"live state unreadable: {type(exc).__name__}"
            ) from None

        _validate_state(state, self.account_identity)
        return state

    def initialize(self, environment, strategy_hash, now_ms=None):
        with self._locked() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            if os.path.exists(self.path):
                raise LiveStateError(
                    "state already exists; refusing to overwrite"
                )

            state = create_initial_state(
                self.account_identity,
                environment,
                strategy_hash,
                now_ms,
            )
            self._atomic_write(state)
            return state

    def _atomic_write(self, state):
        _validate_state(state, self.account_identity)

        directory = os.path.dirname(self.path)
        fd, temp_path = tempfile.mkstemp(
            prefix=".live-state-",
            suffix=".tmp",
            dir=directory,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    state,
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temp_path, self.path)

            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _update(self, event_name, mutator, now_ms=None):
        timestamp = int(time.time() * 1000) if now_ms is None else now_ms

        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("now_ms must be an integer")

        with self._locked() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            state = self.load()
            updated = copy.deepcopy(state)

            mutator(updated)

            updated["sequence"] += 1
            updated["updated_at_ms"] = timestamp
            updated["last_event"] = event_name
            updated = _seal(updated)

            self._atomic_write(updated)
            return updated

    def record_fills(self, fills, now_ms=None):
        """Record a complete fill history and rebuild positions from it."""

        positions = derive_positions_from_fills(fills)
        trade_ids = sorted(
            row["trade_id"] for row in (
                _canonical_fill(fill) for fill in fills
            )
        )

        def mutate(state):
            existing_ids = set(state["processed_trade_ids"])
            incoming_ids = set(trade_ids)

            if existing_ids and not existing_ids.issubset(incoming_ids):
                raise LiveStateError(
                    "fill history regressed; missing previously processed fills"
                )

            state["processed_trade_ids"] = trade_ids
            state["positions"] = positions

        return self._update("FILL_HISTORY_REBUILT", mutate, now_ms)

    def record_balance_snapshot(self, snapshot, now_ms=None):
        if not isinstance(snapshot, dict):
            raise ValueError("balance snapshot must be an object")

        required = ("asset", "wallet_balance", "available_balance")

        for key in required:
            if key not in snapshot:
                raise ValueError(f"balance snapshot missing {key}")

        wallet = _decimal(snapshot["wallet_balance"], "wallet_balance")
        available = _decimal(
            snapshot["available_balance"], "available_balance"
        )

        if available > wallet:
            raise LiveStateError(
                "available balance exceeds wallet balance"
            )

        clean = {
            "asset": _required_text(snapshot["asset"], "asset", 16),
            "wallet_balance": str(wallet),
            "available_balance": str(available),
            "exchange_timestamp_ms": snapshot.get(
                "exchange_timestamp_ms"
            ),
        }

        def mutate(state):
            state["balance_snapshot"] = clean

        return self._update("BALANCE_SNAPSHOT_RECORDED", mutate, now_ms)

    def record_reconciliation(self, result, now_ms=None):
        if not isinstance(result, dict):
            raise ValueError("reconciliation result must be an object")

        ok = result.get("ok")
        halt = result.get("halt_trading")

        if not isinstance(ok, bool) or not isinstance(halt, bool):
            raise LiveStateError(
                "reconciliation must contain boolean ok and halt_trading"
            )

        clean = {
            "ok": ok,
            "halt_trading": halt,
            "reasons": result.get("reasons", []),
            "exchange_timestamp_ms": result.get(
                "exchange_timestamp_ms"
            ),
        }

        def mutate(state):
            state["reconciliation_snapshot"] = clean

            if not ok or halt:
                state["halted"] = True
                state["halt_reason"] = "reconciliation_failed"
            elif state["halt_reason"] == "initial_state_requires_reconciliation":
                state["halted"] = False
                state["halt_reason"] = None

        return self._update("RECONCILIATION_RECORDED", mutate, now_ms)

    def halt(self, reason, now_ms=None):
        reason = _required_text(reason, "reason", 512)

        def mutate(state):
            state["halted"] = True
            state["halt_reason"] = reason

        return self._update("MANUAL_HALT", mutate, now_ms)