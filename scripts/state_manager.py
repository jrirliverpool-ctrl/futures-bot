"""
state_manager.py — مدیریت State و Trade Log برای ApexQuant.

مسئولیت‌ها:
  - Atomic write برای state.json
  - Schema validation
  - strategy_hash match check هنگام load (strict mode)
  - Idempotent append به trade_log.csv (fingerprint-based dedup)
  - Global monotonic event_seq (assigned internally)
  - CSV header/schema validation
  - Timestamp robustness

خارج از مسئولیت:
  - تصمیم‌گیری استراتژی
  - مدیریت پوزیشن

Spec: docs/strategy_v1.md — Section 12
"""

import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from strategy_config import STRATEGY_CONFIG
from compute_hash import compute_strategy_hash


# ============================================================
# Constants
# ============================================================

STATE_SCHEMA_VERSION = "1.0.0"

TRADE_LOG_COLUMNS = (
    "event_id",
    "event_seq",
    "strategy_hash",
    "strategy_version",
    "candle_ts_ms",
    "event_type",
    "side",
    "size",
    "price",
    "entry_price",
    "sl",
    "tp1",
    "exit_price_adj",
    "pnl_usd",
    "remaining_size",
    "reason",
    "ambiguous_exit",
    "timestamp_iso",
)


_REQUIRED_STATE_FIELDS = (
    "version",
    "strategy_version",
    "strategy_hash",
    "initial_equity",
    "equity",
    "peak_equity",
    "last_processed_candle_ts",
    "position",
    "pending_entry",
    "cooldown_until_index",
    "daily_loss",
    "weekly_loss",
    "loss_day_key",
    "loss_week_key",
    "circuit_breaker_active",
)


# ============================================================
# Default State
# ============================================================

def default_state(initial_equity: float = 1000.0) -> dict:
    """
    State اولیه با metadata استراتژی.

    Raises:
        ValueError: اگر initial_equity نامعتبر باشد.
    """
    if isinstance(initial_equity, bool):
        raise ValueError("initial_equity must be numeric, not bool")
    if not isinstance(initial_equity, (int, float)):
        raise ValueError(
            f"initial_equity must be numeric, got "
            f"{type(initial_equity).__name__}"
        )
    if initial_equity <= 0:
        raise ValueError(
            f"initial_equity must be > 0, got {initial_equity}"
        )

    return {
        "version": STATE_SCHEMA_VERSION,
        "strategy_version": STRATEGY_CONFIG["version"],
        "strategy_hash": compute_strategy_hash(),

        "initial_equity": float(initial_equity),
        "equity": float(initial_equity),
        "peak_equity": float(initial_equity),

        "last_processed_candle_ts": None,
        "position": None,
        "pending_entry": None,
        "cooldown_until_index": None,

        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "loss_day_key": None,
        "loss_week_key": None,

        "circuit_breaker_active": False,
    }


# ============================================================
# Schema Validation
# ============================================================

def validate_state(state: dict) -> None:
    """
    Raises ValueError اگر state نامعتبر باشد.
    """
    if not isinstance(state, dict):
        raise ValueError(
            f"state must be dict, got {type(state).__name__}"
        )

    for field in _REQUIRED_STATE_FIELDS:
        if field not in state:
            raise ValueError(f"missing required state field: {field}")

    if state["version"] != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"state schema version mismatch: expected "
            f"{STATE_SCHEMA_VERSION}, got {state['version']}"
        )

    for f in (
        "initial_equity", "equity", "peak_equity",
        "daily_loss", "weekly_loss",
    ):
        v = state[f]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(
                f"{f} must be numeric, got {type(v).__name__}"
            )

    if state["initial_equity"] <= 0:
        raise ValueError("initial_equity must be > 0")
    if state["equity"] < 0:
        raise ValueError("equity cannot be negative")
    if state["daily_loss"] < 0 or state["weekly_loss"] < 0:
        raise ValueError("loss values cannot be negative")

    if not isinstance(state["circuit_breaker_active"], bool):
        raise ValueError("circuit_breaker_active must be bool")

    if state["position"] is not None and not isinstance(state["position"], dict):
        raise ValueError("position must be None or dict")
    if state["pending_entry"] is not None and not isinstance(state["pending_entry"], dict):
        raise ValueError("pending_entry must be None or dict")

    cd = state["cooldown_until_index"]
    if cd is not None and (isinstance(cd, bool) or not isinstance(cd, int)):
        raise ValueError("cooldown_until_index must be None or int")

    lpct = state["last_processed_candle_ts"]
    if lpct is not None and (isinstance(lpct, bool) or not isinstance(lpct, int)):
        raise ValueError("last_processed_candle_ts must be None or int")


# ============================================================
# Atomic JSON Write
# ============================================================

def _atomic_write_json(path: str, data: dict) -> None:
    """نوشتن atomic JSON: tempfile → fsync → os.replace."""
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(dir_, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=dir_, prefix=".state_", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data, f,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


# ============================================================
# Public: State
# ============================================================

def save_state(path: str, state: dict) -> None:
    """ذخیره atomic + schema validation."""
    validate_state(state)
    _atomic_write_json(path, state)


def load_state(path: str, strict: bool = True) -> dict:
    """
    خواندن state.

    Args:
        strict: اگر True (پیش‌فرض)، strategy_hash باید با hash فعلی
                استراتژی مطابق باشد؛ وگرنه ValueError.

    Raises:
        FileNotFoundError, ValueError, json.JSONDecodeError
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"state file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    validate_state(state)

    if strict:
        current = compute_strategy_hash()
        if state["strategy_hash"] != current:
            raise ValueError(
                "\n"
                "╔══════════════════════════════════════════════════╗\n"
                "║  ❌  STATE BELONGS TO A DIFFERENT STRATEGY      ║\n"
                "╚══════════════════════════════════════════════════╝\n"
                f"  state hash   : {state['strategy_hash']}\n"
                f"  current hash : {current}\n"
                "\n"
                "  اگر این عمدی است (migration):\n"
                "    load_state(path, strict=False)\n"
                "╚══════════════════════════════════════════════════╝\n"
            )
    return state


def load_or_init_state(path: str, initial_equity: float = 1000.0,
                       strict: bool = True) -> dict:
    """اگر فایل هست، بخوان؛ وگرنه بساز."""
    if os.path.exists(path):
        return load_state(path, strict=strict)
    state = default_state(initial_equity)
    save_state(path, state)
    return state


def state_exists(path: str) -> bool:
    return os.path.exists(path)


def delete_state(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


# ============================================================
# Trade Log — Event Fingerprint
# ============================================================

def _event_fingerprint(event: dict, strategy_hash: str) -> str:
    """
    fingerprint قطعی برای یک event.

    dedup key از فیلدهای معنایی event + strategy_hash.
    """
    payload = {
        "strategy_hash": strategy_hash,
        "candle_ts_ms": event.get("timestamp"),
        "event_type": event.get("type"),
        "side": event.get("side"),
        "price": event.get("price"),
        "size": event.get("size"),
        "pnl_usd": event.get("pnl_usd"),
        "reason": event.get("reason"),
        "ambiguous": event.get("ambiguous"),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ============================================================
# Trade Log — Row Conversion
# ============================================================

def _timestamp_to_iso(ts_ms) -> str:
    """timestamp ms → ISO UTC. اگر None/خالی → ""."""
    if ts_ms is None or ts_ms == "":
        return ""
    if isinstance(ts_ms, bool):
        raise ValueError("timestamp cannot be bool")
    if not isinstance(ts_ms, (int, float)):
        raise ValueError(
            f"timestamp must be numeric, got {type(ts_ms).__name__}"
        )
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    except (OSError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid timestamp: {ts_ms}") from exc
    return dt.isoformat()


def _row_from_event(event: dict, strategy_hash: str,
                    event_seq: int, event_id: str) -> dict:
    """نرمال‌سازی event به ردیف CSV با schema ثابت."""
    return {
        "event_id": event_id,
        "event_seq": event_seq,
        "strategy_hash": strategy_hash,
        "strategy_version": STRATEGY_CONFIG["version"],
        "candle_ts_ms": (
            event.get("timestamp")
            if event.get("timestamp") is not None else ""
        ),
        "event_type": event.get("type", ""),
        "side": event.get("side", ""),
        "size": event.get("size", ""),
        "price": event.get("price", ""),
        "entry_price": event.get("entry_price", ""),
        "sl": event.get("sl", event.get("new_sl", "")),
        "tp1": event.get("tp1", ""),
        "exit_price_adj": event.get("exit_price_adj", ""),
        "pnl_usd": event.get("pnl_usd", ""),
        "remaining_size": event.get("remaining_size", ""),
        "reason": event.get("reason", ""),
        "ambiguous_exit": event.get("ambiguous", ""),
        "timestamp_iso": _timestamp_to_iso(event.get("timestamp")),
    }


# ============================================================
# Trade Log — Read Helpers
# ============================================================

def _read_existing_ids_and_max_seq(path: str):
    """Returns (set_of_event_ids, max_event_seq)."""
    if not os.path.exists(path):
        return set(), 0

    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != TRADE_LOG_COLUMNS:
            raise ValueError(
                f"trade_log header mismatch.\n"
                f"  expected: {TRADE_LOG_COLUMNS}\n"
                f"  got     : {tuple(reader.fieldnames or ())}"
            )
        ids = set()
        max_seq = 0
        for row in reader:
            eid = row.get("event_id", "")
            if eid:
                ids.add(eid)
            try:
                s = int(row.get("event_seq", 0))
                if s > max_seq:
                    max_seq = s
            except (ValueError, TypeError):
                pass
    return ids, max_seq


# ============================================================
# Trade Log — Append
# ============================================================

def append_trade_events(path: str, events: list,
                        strategy_hash: str = None) -> tuple:
    """
    افزودن idempotent یک دسته event به trade_log.csv.

    - event_id از fingerprint محتوا تولید می‌شود.
    - اگر event با همان fingerprint قبلاً ثبت شده → skip.
    - event_seq به‌صورت global monotonic (از max موجود +1) تخصیص می‌یابد.
    - header در اولین append نوشته می‌شود.

    Returns:
        (appended_count, skipped_count)
    """
    if strategy_hash is None:
        strategy_hash = compute_strategy_hash()

    existing_ids, max_seq = _read_existing_ids_and_max_seq(path)

    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(dir_, exist_ok=True)

    file_exists = os.path.exists(path)
    appended = 0
    skipped = 0

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=TRADE_LOG_COLUMNS,
            extrasaction="ignore",
        )
        if not file_exists:
            writer.writeheader()

        for ev in events:
            event_id = _event_fingerprint(ev, strategy_hash)
            if event_id in existing_ids:
                skipped += 1
                continue

            max_seq += 1
            row = _row_from_event(
                ev, strategy_hash, event_seq=max_seq,
                event_id=event_id,
            )
            writer.writerow(row)
            existing_ids.add(event_id)
            appended += 1

        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    return appended, skipped


# ============================================================
# Trade Log — Load
# ============================================================

def load_trade_log(path: str, validate_header: bool = True) -> list:
    """
    خواندن trade_log با validation header.

    Raises:
        ValueError: اگر header با TRADE_LOG_COLUMNS مطابق نباشد.
    """
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if validate_header:
            actual = tuple(reader.fieldnames or ())
            if actual != TRADE_LOG_COLUMNS:
                raise ValueError(
                    f"trade_log header mismatch.\n"
                    f"  expected: {TRADE_LOG_COLUMNS}\n"
                    f"  got     : {actual}"
                )
        return list(reader)


def trade_log_exists(path: str) -> bool:
    return os.path.exists(path)


def delete_trade_log(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)