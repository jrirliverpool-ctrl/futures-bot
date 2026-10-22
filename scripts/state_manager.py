"""state_manager.py — Atomic State validation with strict numeric checks."""
import json
import math
import os
import tempfile

def default_state(initial_equity=1000.0):
    """Returns a fresh default state dictionary (used by backtest.py)."""
    return {
        "version": "1.0.0",
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
    
    # ✅ بررسی بازگشتی تمام مقادیر برای NaN و Infinity
    _check_finite_values(state, "state")

    # بررسی ساختار پایه
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

def save_state(state_dir, state):
    validate_state(state)
    os.makedirs(state_dir, exist_ok=True)
    target = os.path.join(state_dir, "state.json")
    
    fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".state_", suffix=".tmp")
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

def load_state(state_dir, strict=True):
    path = os.path.join(state_dir, "state.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"State not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    validate_state(state)
    return state