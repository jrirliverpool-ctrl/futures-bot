"""paper_state_manager.py — Strict JSONL handling for trades and events."""
import json
import os
import logging
import hashlib
from datetime import datetime, timezone

logger = logging.getLogger("PaperStateManager")


def state_file(state_dir):
    return os.path.join(state_dir, "state.json")


def events_file(state_dir):
    return os.path.join(state_dir, "events.jsonl")


def _fingerprint(event, strategy_hash):
    """✅ تولید هش ۱۶ کاراکتری یکتا برای هر رویداد"""
    payload = json.dumps(event, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(f"{payload}{strategy_hash}".encode("utf-8")).hexdigest()[:16]


def init_state(state_dir, initial_equity=1000.0, symbol="BTC-SWAP-USDT"):
    from state_manager import save_state, default_state, load_state
    if state_exists(state_dir):
        return load_state(state_dir, strict=False)
    state = default_state(initial_equity)
    state["symbol"] = symbol
    save_state(state_dir, state)
    return state


def state_exists(state_dir):
    return os.path.exists(state_file(state_dir))


def load_state(state_dir, strict=True):
    from state_manager import load_state as _load
    return _load(state_dir, strict=strict)


def save_state(state_dir, state):
    from state_manager import save_state as _save
    _save(state_dir, state)


def load_events(state_dir):
    events_path = events_file(state_dir)
    if not os.path.exists(events_path):
        return []

    events = []
    with open(events_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"Skipping corrupted line {line_num} in {events_path}")
                continue
    return events


def append_events(state_dir, events, strategy_hash=None):
    if not events:
        return 0, 0

    from compute_hash import compute_strategy_hash, get_strategy_metadata
    if strategy_hash is None:
        strategy_hash = compute_strategy_hash()
    metadata = get_strategy_metadata()

    events_path = events_file(state_dir)
    existing_ids = set()

    if os.path.exists(events_path):
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try: existing_ids.add(json.loads(line).get("event_id"))
                    except: pass

    appended = 0
    skipped = 0
    os.makedirs(state_dir, exist_ok=True)
    
    with open(events_path, "a", encoding="utf-8") as f:
        for ev in events:
            ev = ev.copy()
            
            # ✅ تبدیل type به event_type طبق نیاز تست
            if "type" in ev:
                ev["event_type"] = ev.pop("type")
                
            # ✅ تبدیل timestamp به candle_ts_ms و timestamp_iso
            if "timestamp" in ev:
                ts = ev.pop("timestamp")
                ev["candle_ts_ms"] = ts
                ev["timestamp_iso"] = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).isoformat()
                
            ev["strategy_hash"] = strategy_hash
            ev["strategy_version"] = metadata["version"]
            
            ev_id = _fingerprint(ev, strategy_hash)
            
            if ev_id in existing_ids:
                skipped += 1
                continue

            ev["event_id"] = ev_id
            ev["seq"] = len(existing_ids) + appended + 1
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            existing_ids.add(ev_id)
            appended += 1

    return appended, skipped


def events_count(state_dir):
    events_path = events_file(state_dir)
    if not os.path.exists(events_path):
        return 0
    with open(events_path, "r") as f:
        return sum(1 for _ in f)


def max_seq(events):
    if not events:
        return 0
    seqs = []
    for event in events:
        value = event.get("seq", 0)
        if isinstance(value, bool):
            raise ValueError("Invalid event seq: boolean is not allowed")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid event seq: {value!r}")
        if number < 0:
            raise ValueError(f"Invalid event seq: {value!r}")
        seqs.append(number)
    return max(seqs, default=0)