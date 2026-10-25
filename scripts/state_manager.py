"""state_manager.py — Atomic State validation with strict numeric checks and strategy hash."""
import json
import math
import os
import tempfile

from compute_hash import compute_strategy_hash, get_strategy_metadata

# ✅ ثابت‌های موردنیاز تست‌ها
STATE_SCHEMA_VERSION = "1.0.0"

# ✅ ستون‌های استاندارد لاگ معاملات (موردنیاز test_state_manager)
TRADE_LOG_COLUMNS = [
    "timestamp", "symbol", "side", "qty", "entry_price", 
    "exit_price", "pnl_usd", "commission_usd", "reason", "equity"
]


def default_state(initial_equity=1000.0):
    """Returns a fresh default state dictionary (used by backtest.py)."""
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


def delete_state(state_dir):
    """حذف فایل state.json به همراه پشتیبان‌های آن."""
    state_path = os.path.join(state_dir, "state.json")
    if os.path.exists(state_path):
        os.remove(state_path)
    for f in os.listdir(state_dir):
        if f.startswith("state.json.bak") or f.startswith(".state_"):
            try:
                os.remove(os.path.join(state_dir, f))
            except OSError:
                pass