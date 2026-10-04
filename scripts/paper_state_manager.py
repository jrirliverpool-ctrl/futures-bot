"""
paper_state_manager.py — Persistence layer برای Paper Trading.

برخلاف state_manager که روی یک فایل کار می‌کند، این ماژول
روی یک دایرکتوری کار می‌کند که شامل:

  state.json      ← snapshot (overwrite)
  events.jsonl    ← append-only log

مسئولیت‌ها:
  - load/save state (atomic، از طریق state_manager)
  - append events به JSONL با idempotent dedup
  - load events و محاسبه max seq

غیرمسئولیت‌ها:
  - تصمیم‌گیری معامله
  - مدیریت پوزیشن

قرارداد event schema
════════════════════
ورودی خام از paper_engine فیلد `timestamp` دارد (میلی‌ثانیه UTC).
در خروجی JSONL، همان مقدار با نام `candle_ts_ms` ذخیره می‌شود.
این تبدیل صریح است تا schema خروجی پایدار و قابل مقایسه باشد.

قرارداد fingerprint
═══════════════════
event_id = 16-char hex از SHA256 روی canonical JSON شامل:
  - strategy_hash
  - event_type
  - timestamp
  - side
  - price
  - size
  - sl (یا new_sl اگر sl نبود)
  - tp1
  - entry_price
  - exit_price_adj
  - pnl_usd
  - remaining_size
  - reason
  - ambiguous

تمام فیلدهای هویت event در fingerprint وارد می‌شوند تا
دو event متفاوت هرگز fingerprint یکسان نگیرند.

Spec: docs/step9a_paper_persistence.md
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from strategy_config import STRATEGY_CONFIG
from compute_hash import compute_strategy_hash
from state_manager import (
    save_state as _save_state_json,
    load_state as _load_state_json,
    state_exists as _state_exists,
    default_state as _default_state,
)


# ============================================================
# Paths
# ============================================================

def state_file(state_dir: str) -> str:
    return os.path.join(state_dir, "state.json")


def events_file(state_dir: str) -> str:
    return os.path.join(state_dir, "events.jsonl")


# ============================================================
# State I/O
# ============================================================

def load_state(state_dir: str, strict: bool = False) -> dict:
    """
    Load state.json از state_dir.

    strict=False به‌صورت پیش‌فرض چون paper-state ممکن است
    با نسخه‌ی متفاوتی از strategy ساخته شده باشد.
    """
    return _load_state_json(state_file(state_dir), strict=strict)


def save_state(state_dir: str, state: dict) -> None:
    """Atomic save (از طریق state_manager)."""
    _save_state_json(state_file(state_dir), state)


def state_exists(state_dir: str) -> bool:
    return _state_exists(state_file(state_dir))


def init_state(state_dir: str, initial_equity: float = 1000.0,
               symbol: str = "BTC-SWAP-USDT") -> dict:
    """
    ساخت state اولیه اگر وجود ندارد.
    Returns: state فعلی (موجود یا تازه‌ساخته‌شده).
    """
    if state_exists(state_dir):
        return load_state(state_dir)
    state = _default_state(initial_equity)
    state["symbol"] = symbol
    save_state(state_dir, state)
    return state


# ============================================================
# Event Fingerprint
# ============================================================

def _fingerprint(event: dict, strategy_hash: str) -> str:
    """
    fingerprint 16-char از محتوای کامل event.

    شامل تمام فیلدهای هویت event تا collision رخ ندهد.
    اگر دو event فقط در یکی از این فیلدها تفاوت داشته باشند،
    fingerprint آن‌ها متفاوت خواهد بود.
    """
    payload = {
        "strategy_hash": strategy_hash,
        "event_type": event.get("type"),
        "timestamp": event.get("timestamp"),
        "side": event.get("side"),
        "price": event.get("price"),
        "size": event.get("size"),
        "sl": event.get("sl", event.get("new_sl")),
        "tp1": event.get("tp1"),
        "entry_price": event.get("entry_price"),
        "exit_price_adj": event.get("exit_price_adj"),
        "pnl_usd": event.get("pnl_usd"),
        "remaining_size": event.get("remaining_size"),
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
# Event Serialization
# ============================================================

def _serialize_event(event: dict, strategy_hash: str,
                     seq: int, event_id: str) -> dict:
    """
    تبدیل event خام به record JSONL با schema ثابت.

    ورودی خام: فیلد `timestamp`
    خروجی JSONL: فیلد `candle_ts_ms`
    """
    ts = event.get("timestamp")

    ts_iso = ""
    if ts is not None and ts != "":
        try:
            ts_iso = datetime.fromtimestamp(
                int(ts) / 1000, tz=timezone.utc,
            ).isoformat()
        except (OSError, ValueError, OverflowError, TypeError):
            ts_iso = ""

    return {
        "event_id": event_id,
        "seq": seq,
        "strategy_hash": strategy_hash,
        "strategy_version": STRATEGY_CONFIG["version"],
        "candle_ts_ms": ts if ts is not None else None,
        "event_type": event.get("type", ""),
        "side": event.get("side"),
        "size": event.get("size"),
        "price": event.get("price"),
        "entry_price": event.get("entry_price"),
        "sl": event.get("sl", event.get("new_sl")),
        "tp1": event.get("tp1"),
        "exit_price_adj": event.get("exit_price_adj"),
        "pnl_usd": event.get("pnl_usd"),
        "remaining_size": event.get("remaining_size"),
        "reason": event.get("reason"),
        "ambiguous_exit": event.get("ambiguous"),
        "timestamp_iso": ts_iso,
    }


# ============================================================
# Events Read/Write
# ============================================================

def load_events(state_dir: str) -> list:
    """
    خواندن تمام events از events.jsonl.
    Returns: list of dict. اگر فایل نبود → [].
    خطوط خالی و خطوط نامعتبر skip می‌شوند.
    """
    path = events_file(state_dir)
    if not os.path.exists(path):
        return []

    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def max_seq(events: list) -> int:
    """بزرگ‌ترین seq موجود. 0 اگر خالی."""
    m = 0
    for e in events:
        try:
            s = int(e.get("seq", 0))
            if s > m:
                m = s
        except (ValueError, TypeError):
            continue
    return m


def append_events(state_dir: str, events: list,
                  strategy_hash: str = None) -> tuple:
    """
    Append events به events.jsonl با idempotent dedup.

    Returns:
        (appended_count, skipped_count)
    """
    if strategy_hash is None:
        strategy_hash = compute_strategy_hash()

    path = events_file(state_dir)
    os.makedirs(state_dir, exist_ok=True)

    existing = load_events(state_dir)
    existing_ids = {e.get("event_id") for e in existing if e.get("event_id")}
    current_max = max_seq(existing)

    appended = 0
    skipped = 0

    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            eid = _fingerprint(ev, strategy_hash)
            if eid in existing_ids:
                skipped += 1
                continue

            current_max += 1
            record = _serialize_event(ev, strategy_hash,
                                      seq=current_max, event_id=eid)
            f.write(json.dumps(
                record,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ))
            f.write("\n")
            existing_ids.add(eid)
            appended += 1

        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    return appended, skipped


def events_count(state_dir: str) -> int:
    """تعداد کل events."""
    return len(load_events(state_dir))