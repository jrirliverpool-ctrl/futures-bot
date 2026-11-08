
"""Independent fail-closed state for Toobit USDT-M execution.

This module never sends exchange requests.
Positions are derived exclusively from complete normalized fills.
Balance and reconciliation evidence must be fresh and complete.

A local atomic file is NOT durable across ephemeral GitHub Actions
runners. Persistent private storage is mandatory before live execution.
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

MAX_SNAPSHOT_AGE_MS = 30_000
MAX_FUTURE_SKEW_MS = 5_000

BALANCE_SOURCE = "TOOBIT_V2_ACCOUNT_BALANCE"
POSITION_SOURCE = "TOOBIT_V2_POSITION_RISK"


class LiveStateError(RuntimeError):
    """Raised when live state or exchange evidence is unsafe."""


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


def _timestamp(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveStateError(f"{field} must be an integer")

    if value <= 0:
        raise LiveStateError(f"{field} must be positive")

    return value


def _check_freshness(exchange_timestamp_ms, now_ms, field):
    exchange_ts = _timestamp(exchange_timestamp_ms, field)

    if exchange_ts > now_ms + MAX_FUTURE_SKEW_MS:
        raise LiveStateError(f"{field} is in the future")

    age = now_ms - exchange_ts

    if age > MAX_SNAPSHOT_AGE_MS:
        raise LiveStateError(f"{field} is stale")

    return age


def _canonical_fill(fill):
    if not isinstance(fill, dict):
        raise LiveStateError("fill must be an object")

    trade_id = _required_text(fill.get("trade_id"), "trade_id")
    symbol = _required_text(fill.get("symbol"), "symbol", 64)
    side = _required_text(fill.get("side"), "side", 32)

    if side not in OPEN_SIDES | CLOSE_SIDES:
        raise LiveStateError(f"unsupported fill side: {side}")

    timestamp = fill.get("timestamp_ms")

    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise LiveStateError("invalid fill timestamp_ms")

    if timestamp <= 0:
        raise LiveStateError("invalid fill timestamp_ms")

    qty = _decimal(fill.get("qty"), "fill.qty", False)
    price = _decimal(fill.get("price"), "fill.price", False)

    sequence = fill.get("exchange_sequence")

    if sequence is not None:
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise LiveStateError("invalid exchange_sequence")
        if sequence < 0:
            raise LiveStateError("invalid exchange_sequence")

    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "timestamp_ms": timestamp,
        "exchange_sequence": sequence,
        "qty": qty,
        "price": price,
    }


def _ordered_fills(fills):
    if not isinstance(fills, list):
        raise LiveStateError("fills must be a list")

    normalized = [_canonical_fill(row) for row in fills]

    seen_ids = set()

    for row in normalized:
        if row["trade_id"] in seen_ids:
            raise LiveStateError(
                f"duplicate trade_id: {row['trade_id']}"
            )
        seen_ids.add(row["trade_id"])

    # Equal timestamps on the same symbol are ambiguous unless
    # every fill has an exchange-provided ordering sequence.
    groups = {}

    for row in normalized:
        key = (row["symbol"], row["timestamp_ms"])
        groups.setdefault(key, []).append(row)

    for key, rows in groups.items():
        if len(rows) <= 1:
            continue

        sequences = [row["exchange_sequence"] for row in rows]

        if any(value is None for value in sequences):
            raise LiveStateError(
                f"ambiguous fill ordering for {key[0]} "
                f"at timestamp {key[1]}"
            )

        if len(set(sequences)) != len(sequences):
            raise LiveStateError(
                f"duplicate exchange sequence for {key[0]}"
            )

    normalized.sort(
        key=lambda row: (
            row["timestamp_ms"],
            row["exchange_sequence"]
            if row["exchange_sequence"] is not None
            else 0,
            row["symbol"],
        )
    )

    return normalized


def derive_positions_from_fills(fills):
    """Rebuild one-way positions from complete ordered fill history."""

    normalized = _ordered_fills(fills)
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
                    f"opposite-side open without closing fill: {symbol}"
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

        expected = (
            "SELL_CLOSE"
            if current["side"] == "LONG"
            else "BUY_CLOSE"
        )

        if side != expected:
            raise LiveStateError(
                f"closing side mismatch for {symbol}"
            )

        old_qty = Decimal(current["quantity"])

        if qty > old_qty:
            raise LiveStateError(
                f"closing quantity exceeds position: {symbol}"
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


def _normalize_position_map(value, field):
    if not isinstance(value, dict):
        raise LiveStateError(f"{field} must be an object")

    result = {}

    for symbol, position in value.items():
        symbol = _required_text(symbol, f"{field}.symbol", 64)

        if not isinstance(position, dict):
            raise LiveStateError(f"{field} position must be an object")

        side = _required_text(
            position.get("side"), f"{field}.side", 16
        )

        if side not in ("LONG", "SHORT"):
            raise LiveStateError(f"{field} invalid position side")

        quantity = _decimal(
            position.get("quantity"),
            f"{field}.quantity",
            False,
        )

        avg_price = _decimal(
            position.get("avg_price"),
            f"{field}.avg_price",
            False,
        )

        result[symbol] = {
            "side": side,
            "quantity": quantity,
            "avg_price": avg_price,
        }

    return result


def _positions_equal(left, right):
    if set(left) != set(right):
        return False

    for symbol in left:
        a = left[symbol]
        b = right[symbol]

        if a["side"] != b["side"]:
            return False

        if a["quantity"] != b["quantity"]:
            return False

        if a["avg_price"] != b["avg_price"]:
            return False

    return True


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

    if not isinstance(state.get("strategy_hash"), str):
        raise LiveStateError("strategy_hash missing")

    if not state["strategy_hash"].strip():
        raise LiveStateError("strategy_hash missing")

    for field in ("sequence", "created_at_ms", "updated_at_ms"):
        value = state.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise LiveStateError(f"invalid {field}")
        if value < 0:
            raise LiveStateError(f"invalid {field}")

    if not isinstance(state.get("halted"), bool):
        raise LiveStateError("halted must be boolean")

    if not isinstance(state.get("halt_reason"), (str, type(None))):
        raise LiveStateError("invalid halt_reason")

    if not isinstance(state.get("positions"), dict):
        raise LiveStateError("positions must be an object")

    if not isinstance(state.get("processed_trade_ids"), list):
        raise LiveStateError("processed_trade_ids must be a list")

    ids = state["processed_trade_ids"]

    if not all(isinstance(x, str) and x for x in ids):
        raise LiveStateError("invalid processed trade ID")

    if len(set(ids)) != len(ids):
        raise LiveStateError("duplicate processed trade IDs")

    if not isinstance(state.get("balance_snapshot"), (dict, type(None))):
        raise LiveStateError("invalid balance_snapshot")

    if not isinstance(
        state.get("reconciliation_snapshot"),
        (dict, type(None)),
    ):
        raise LiveStateError("invalid reconciliation_snapshot")

    supplied = state.get("integrity_sha256")

    if (
        not isinstance(supplied, str)
        or supplied != _state_digest(state)
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
        account_identity, "account_identity"
    )
    strategy_hash = _required_text(strategy_hash, "strategy_hash")

    if environment not in ENVIRONMENTS:
        raise ValueError("environment must be DEMO or LIVE")

    timestamp = int(time.time() * 1000) if now_ms is None else now_ms

    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("now_ms must be an integer")

    if timestamp <= 0:
        raise ValueError("now_ms must be positive")

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
    """Atomic local store. Persistent private storage is still required."""

    def __init__(self, path, account_identity):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("state path must be absolute")

        self.path = path
        self.lock_path = path + ".lock"
        self.account_identity = _required_text(
            account_identity, "account_identity"
        )

    def _locked(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        return open(self.lock_path, "a+", encoding="utf-8")

    def load(self):
        if not os.path.exists(self.path):
            raise LiveStateError(
                "live state file missing; initialization required"
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

        if timestamp <= 0:
            raise ValueError("now_ms must be positive")

        with self._locked() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            state = self.load()

            if timestamp < state["updated_at_ms"]:
                raise LiveStateError("state update timestamp regressed")

            updated = copy.deepcopy(state)
            mutator(updated)

            updated["sequence"] += 1
            updated["updated_at_ms"] = timestamp
            updated["last_event"] = event_name

            updated = _seal(updated)
            self._atomic_write(updated)

            return updated

    def record_fills(self, fills, now_ms=None):
        normalized = _ordered_fills(fills)
        positions = derive_positions_from_fills(fills)

        trade_ids = sorted(
            row["trade_id"] for row in normalized
        )

        def mutate(state):
            existing = set(state["processed_trade_ids"])
            incoming = set(trade_ids)

            if existing and not existing.issubset(incoming):
                raise LiveStateError(
                    "fill history regressed; previously processed "
                    "fills are missing"
                )

            state["processed_trade_ids"] = trade_ids
            state["positions"] = positions

        return self._update(
            "FILL_HISTORY_REBUILT", mutate, now_ms
        )

    def record_balance_snapshot(self, snapshot, now_ms=None):
        if not isinstance(snapshot, dict):
            raise LiveStateError("balance snapshot must be an object")

        now = int(time.time() * 1000) if now_ms is None else now_ms

        if isinstance(now, bool) or not isinstance(now, int):
            raise ValueError("now_ms must be an integer")

        source = _required_text(snapshot.get("source"), "balance.source")

        if source != BALANCE_SOURCE:
            raise LiveStateError("untrusted balance source")

        exchange_ts = _timestamp(
            snapshot.get("exchange_timestamp_ms"),
            "balance.exchange_timestamp_ms",
        )

        _check_freshness(exchange_ts, now, "balance timestamp")

        asset = _required_text(snapshot.get("asset"), "balance.asset", 16)

        if asset != "USDT":
            raise LiveStateError("unexpected balance asset")

        wallet = _decimal(
            snapshot.get("wallet_balance"), "wallet_balance"
        )
        available = _decimal(
            snapshot.get("available_balance"), "available_balance"
        )

        if available > wallet:
            raise LiveStateError(
                "available balance exceeds wallet balance"
            )

        clean = {
            "source": source,
            "asset": asset,
            "wallet_balance": str(wallet),
            "available_balance": str(available),
            "exchange_timestamp_ms": exchange_ts,
            "received_at_ms": now,
        }

        def mutate(state):
            previous = state.get("balance_snapshot")

            if previous is not None:
                old_ts = previous.get("exchange_timestamp_ms", 0)

                if exchange_ts < old_ts:
                    raise LiveStateError(
                        "balance exchange timestamp regressed"
                    )

            state["balance_snapshot"] = clean

        return self._update(
            "BALANCE_SNAPSHOT_RECORDED", mutate, now
        )

    def record_reconciliation(self, result, now_ms=None):
        if not isinstance(result, dict):
            raise LiveStateError("reconciliation must be an object")

        now = int(time.time() * 1000) if now_ms is None else now_ms

        if isinstance(now, bool) or not isinstance(now, int):
            raise ValueError("now_ms must be an integer")

        if not isinstance(result.get("ok"), bool):
            raise LiveStateError("reconciliation ok must be boolean")

        if not isinstance(result.get("halt_trading"), bool):
            raise LiveStateError(
                "reconciliation halt_trading must be boolean"
            )

        source = _required_text(
            result.get("source"), "reconciliation.source"
        )

        if source != POSITION_SOURCE:
            raise LiveStateError("untrusted reconciliation source")

        exchange_ts = _timestamp(
            result.get("exchange_timestamp_ms"),
            "reconciliation.exchange_timestamp_ms",
        )

        _check_freshness(
            exchange_ts, now, "reconciliation timestamp"
        )

        expected = result.get("symbols_expected")
        checked = result.get("symbols_checked")

        if (
            not isinstance(expected, list)
            or not isinstance(checked, list)
            or not expected
        ):
            raise LiveStateError("symbol coverage is missing")

        expected = [
            _required_text(x, "expected symbol", 64)
            for x in expected
        ]

        checked = [
            _required_text(x, "checked symbol", 64)
            for x in checked
        ]

        if len(set(expected)) != len(expected):
            raise LiveStateError("duplicate expected symbols")

        if len(set(checked)) != len(checked):
            raise LiveStateError("duplicate checked symbols")

        if set(expected) != set(checked):
            raise LiveStateError(
                "reconciliation symbol coverage incomplete"
            )

        exchange_positions = _normalize_position_map(
            result.get("exchange_positions"),
            "exchange_positions",
        )

        if not set(exchange_positions).issubset(set(expected)):
            raise LiveStateError(
                "exchange position contains unexpected symbol"
            )

        reasons = result.get("reasons", [])

        if not isinstance(reasons, list):
            raise LiveStateError("reconciliation reasons must be a list")

        if not all(isinstance(x, str) for x in reasons):
            raise LiveStateError("invalid reconciliation reason")

        def mutate(state):
            balance = state.get("balance_snapshot")

            if balance is None:
                raise LiveStateError(
                    "fresh balance required before reconciliation"
                )

            balance_age = now - balance["exchange_timestamp_ms"]

            if (
                balance_age < -MAX_FUTURE_SKEW_MS
                or balance_age > MAX_SNAPSHOT_AGE_MS
            ):
                raise LiveStateError(
                    "balance snapshot is stale during reconciliation"
                )

            local_positions = _normalize_position_map(
                state["positions"], "local_positions"
            )

            if not set(local_positions).issubset(set(expected)):
                raise LiveStateError(
                    "local positions include unchecked symbols"
                )

            if not _positions_equal(
                local_positions, exchange_positions
            ):
                raise LiveStateError(
                    "exchange and local positions do not match"
                )

            if result["ok"] is not True or result["halt_trading"] is not False:
                state["halted"] = True
                state["halt_reason"] = "reconciliation_failed"
            elif reasons:
                state["halted"] = True
                state["halt_reason"] = "reconciliation_has_reasons"
            elif state["halt_reason"] == "initial_state_requires_reconciliation":
                state["halted"] = False
                state["halt_reason"] = None

            state["reconciliation_snapshot"] = {
                "source": source,
                "ok": result["ok"],
                "halt_trading": result["halt_trading"],
                "reasons": reasons,
                "symbols_expected": sorted(expected),
                "symbols_checked": sorted(checked),
                "exchange_timestamp_ms": exchange_ts,
                "received_at_ms": now,
            }

        return self._update(
            "RECONCILIATION_RECORDED", mutate, now
        )

    def halt(self, reason, now_ms=None):
        reason = _required_text(reason, "reason", 512)

        def mutate(state):
            state["halted"] = True
            state["halt_reason"] = reason

        return self._update("MANUAL_HALT", mutate, now_ms)
