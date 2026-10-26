"""state_manager.py — Atomic State validation with strict numeric checks and strategy hash."""
import csv
import json
import math
import os
import tempfile
from datetime import datetime, timezone

from compute_hash import compute_strategy_hash, get_strategy_metadata

STATE_SCHEMA_VERSION = "1.0.0"

TRADE_LOG_COLUMNS = [
    "event_type", "event_seq", "timestamp_iso", "symbol", "side", 
    "qty", "entry_price", "exit_price", "pnl_usd", "commission_usd", 
    "reason", "equity", "strategy_version", "strategy_hash"
]


def _get_state_path(path):
    if path.endswith(".json"):
        return path
    return os.path.join(path, "state.json")


def default_state(initial_equity=1000.0):
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


def state_exists(path):
    target = _get_state_path(path)
    return os.path.exists(target)


def trade_log_exists(csv_path):
    """✅ بررسی وجود فایل لاگ معاملات (اضافه شده برای رفع خطای تست)"""
    return os.path.exists(csv_path)


def load_or_init_state(path, initial_equity=1000.0, symbol="UNKNOWN"):
    state_path = _get_state_path(path)
    if os.path.exists(state_path):
        return load_state(path, strict=True)
    state = default_state(initial_equity)
    state["symbol"] = symbol
    save_state(path, state)
    return state


def validate_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError("state must be a dict")
    _check_finite_values(state, "state")
    required = ["initial_equity", "equity", "peak_equity"]
    for k in required:
        if k not in state:
            raise ValueError(f"Missing required key: {k}")
        if not isinstance(state[k], (int, float)) or isinstance(state[k], bool):
            raise ValueError(f"{k} must be a number")


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
            raise ValueError(f"State missing 'strategy_hash'. Expected: {current_hash}.")
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


def append_trade_events(csv_path, events, strategy_metadata=None):
    if not events: return
    file_exists = os.path.exists(csv_path)
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS, extrasaction='ignore')
        if not file_exists:
            writer.writeheader()
            
        for ev in events:
            row = {col: "" for col in TRADE_LOG_COLUMNS}
            row["event_type"] = ev.get("type", "UNKNOWN")
            row["event_seq"] = ev.get("seq", 0)
            
            ts = ev.get("timestamp")
            if ts:
                row["timestamp_iso"] = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).isoformat()
                
            row["symbol"] = ev.get("symbol", "UNKNOWN")
            row["side"] = ev.get("side", "")
            row["qty"] = ev.get("size", ev.get("qty", ""))
            row["entry_price"] = ev.get("entry_price", ev.get("price", ""))
            row["exit_price"] = ev.get("exit_price_adj", ev.get("price", ""))
            row["pnl_usd"] = ev.get("pnl_usd", "")
            row["commission_usd"] = ev.get("commission_usd", "")
            row["reason"] = ev.get("reason", "")
            row["equity"] = ev.get("equity", "")
            
            if strategy_metadata:
                row["strategy_version"] = strategy_metadata.get("version", "")
                row["strategy_hash"] = strategy_metadata.get("hash", "")
                
            writer.writerow(row)


def load_trade_log(csv_path):
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)