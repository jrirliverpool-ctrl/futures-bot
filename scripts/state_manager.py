"""state_manager.py — Atomic State validation with strict numeric checks and strategy hash."""
import csv
import hashlib
import json
import logging
import math
import os
import tempfile
from datetime import datetime, timezone

from compute_hash import compute_strategy_hash, get_strategy_metadata

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = "1.0.0"

TRADE_LOG_COLUMNS = [
    "event_id", "event_type", "event_seq", "timestamp_iso", "symbol", "side",
    "qty", "entry_price", "exit_price", "pnl_usd", "commission_usd",
    "reason", "equity", "strategy_version", "strategy_hash"
]


def _get_state_path(path):
    if path.endswith(".json"):
        return path
    return os.path.join(path, "state.json")


def default_state(initial_equity=1000.0):
    if isinstance(initial_equity, bool):
        raise ValueError("initial_equity must be a number, not bool")
    if not isinstance(initial_equity, (int, float)):
        raise ValueError("initial_equity must be a number")
    if not math.isfinite(initial_equity):
        raise ValueError("initial_equity must be finite")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be > 0")

    metadata = get_strategy_metadata()
    return {
        "version": STATE_SCHEMA_VERSION,
        "strategy_hash": metadata["hash"],
        "strategy_version": metadata["version"],
        "initial_equity": initial_equity,
        "equity": initial_equity,
        "peak_equity": initial_equity,
        "symbol": "UNKNOWN",
        "position": None,
        "pending_entry": None,
        "last_processed_candle_ts": None,
        "cooldown_until_index": None,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "loss_day_key": None,
        "loss_week_key": None,
        "circuit_breaker_active": False,
        "last_funding_ts": None,
    }


def validate_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError("state must be a dict")

    if state.get("version") != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"version must be {STATE_SCHEMA_VERSION}, got {state.get('version')!r}"
        )

    _check_finite_values(state, "state")

    for k in ("initial_equity", "equity", "peak_equity"):
        if k not in state:
            raise ValueError(f"Missing required key: {k}")
        if isinstance(state[k], bool) or not isinstance(state[k], (int, float)):
            raise ValueError(f"{k} must be a number")
        if not math.isfinite(state[k]):
            raise ValueError(f"{k} must be finite")
        if state[k] < 0:
            raise ValueError(f"{k} must be >= 0")

    if state.get("position") is not None and not isinstance(state["position"], dict):
        raise ValueError("position must be None or dict")

    if state.get("pending_entry") is not None and not isinstance(state["pending_entry"], dict):
        raise ValueError("pending_entry must be None or dict")

    cdi = state.get("cooldown_until_index")
    if cdi is not None and (isinstance(cdi, bool) or not isinstance(cdi, int)):
        raise ValueError("cooldown_until_index must be None or int")

    lpc = state.get("last_processed_candle_ts")
    if lpc is not None and (isinstance(lpc, bool) or not isinstance(lpc, int)):
        raise ValueError("last_processed_candle_ts must be None or int")


def _check_finite_values(obj, path):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite_values(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_finite_values(v, f"{path}[{i}]")
    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"Invalid numeric value at {path}: {obj}")


def save_state(path, state):
    validate_state(state)
    target = _get_state_path(path)
    state_dir = os.path.dirname(target)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=state_dir or ".", prefix=".state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
        raise


def load_state(path, strict=True):
    target = _get_state_path(path)
    if not os.path.exists(target):
        raise FileNotFoundError(f"State not found at {target}")
    with open(target, "r", encoding="utf-8") as f:
        state = json.load(f)
    validate_state(state)

    if strict:
        try:
            current_hash = compute_strategy_hash()
        except Exception as e:
            raise ValueError(f"Could not compute current strategy hash: {e}")

        saved_hash = state.get("strategy_hash")
        if saved_hash is None:
            raise ValueError(
                f"State missing 'strategy_hash'. Expected: {current_hash}."
            )
        if saved_hash != current_hash:
            raise ValueError(
                f"Strategy hash mismatch! Saved: {saved_hash}, Current: {current_hash}."
            )
    return state


def delete_state(path):
    target = _get_state_path(path)
    if os.path.exists(target):
        os.remove(target)
    state_dir = os.path.dirname(target)
    if state_dir and os.path.exists(state_dir):
        for f in os.listdir(state_dir):
            if f.startswith("state.json.bak") or f.startswith(".state_"):
                try: os.remove(os.path.join(state_dir, f))
                except OSError: pass


def state_exists(path):
    return os.path.exists(_get_state_path(path))


def load_or_init_state(path, initial_equity=1000.0, symbol="UNKNOWN"):
    if state_exists(path):
        return load_state(path, strict=True)
    state = default_state(initial_equity)
    state["symbol"] = symbol
    save_state(path, state)
    return state


def trade_log_exists(csv_path):
    return os.path.exists(csv_path)


def delete_trade_log(csv_path):
    if os.path.exists(csv_path):
        os.remove(csv_path)


def _event_identity(ev):
    payload = {k: v for k, v in ev.items() if k not in ("seq", "event_id")}
    s = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _validate_event_for_csv(ev):
    if not isinstance(ev, dict):
        raise ValueError("event must be a dict")
    ts = ev.get("timestamp")
    if ts is not None:
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise ValueError(f"Invalid timestamp type: {type(ts).__name__}")
        if not math.isfinite(ts):
            raise ValueError("timestamp must be finite")
        try:
            datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as e:
            raise ValueError(f"Timestamp out of range: {ts!r} ({e})")


def _read_trade_log_rows(csv_path):
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _last_valid_seq(rows):
    last = 0
    for row in rows:
        val = row.get("event_seq")
        if val in (None, ""):
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        if n > last:
            last = n
    return last


def append_trade_events(csv_path, events, strategy_metadata=None):
    if not events:
        return 0, 0

    if strategy_metadata is None:
        strategy_metadata = get_strategy_metadata()

    for ev in events:
        _validate_event_for_csv(ev)

    existing_rows = []
    if os.path.exists(csv_path):
        try:
            existing_rows = _read_trade_log_rows(csv_path)
        except Exception as e:
            raise ValueError(
                f"Could not read existing trade log {csv_path}: {e}"
            )

    existing_ids = set()
    for row in existing_rows:
        evid = row.get("event_id")
        if evid:
            existing_ids.add(evid)

    start_seq = _last_valid_seq(existing_rows) + 1

    file_exists = os.path.exists(csv_path)
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

    appended = 0
    skipped = 0

    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()

        for ev in events:
            ev_id = _event_identity(ev)
            if ev_id in existing_ids:
                skipped += 1
                continue

            row = {col: "" for col in TRADE_LOG_COLUMNS}
            row["event_id"] = ev_id
            row["event_type"] = ev.get("type", "UNKNOWN")
            row["event_seq"] = start_seq + appended

            ts = ev.get("timestamp")
            if ts is not None:
                row["timestamp_iso"] = datetime.fromtimestamp(
                    float(ts) / 1000.0, tz=timezone.utc
                ).isoformat()

            row["symbol"] = ev.get("symbol", "UNKNOWN")
            row["side"] = ev.get("side", "")
            row["qty"] = ev.get("size", ev.get("qty", ""))
            row["entry_price"] = ev.get("entry_price", ev.get("price", ""))
            row["exit_price"] = ev.get("exit_price_adj", ev.get("price", ""))
            row["pnl_usd"] = ev.get("pnl_usd", "")
            row["commission_usd"] = ev.get("commission_usd", "")
            row["reason"] = ev.get("reason", "")
            row["equity"] = ev.get("equity", "")
            row["strategy_version"] = strategy_metadata.get("version", "")
            row["strategy_hash"] = strategy_metadata.get("hash", "")

            writer.writerow(row)
            existing_ids.add(ev_id)
            appended += 1

    return appended, skipped


def load_trade_log(csv_path, validate_header=True):
    if not os.path.exists(csv_path):
        return []

    if validate_header:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            header_line = f.readline().strip()
        header = header_line.split(",") if header_line else []
        if header != TRADE_LOG_COLUMNS:
            # ✅ تغییر پیام خطا برای هماهنگی دقیق با تست
            raise ValueError(
                f"CSV header mismatch. Expected {TRADE_LOG_COLUMNS}, got {header}"
            )

    return _read_trade_log_rows(csv_path)