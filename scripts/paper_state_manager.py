"""paper_state_manager.py — Strict JSONL handling for trades and events."""
import json
import os
import logging

logger = logging.getLogger("PaperStateManager")


def state_file(state_dir):
    """Returns the path to the state.json file in the given directory."""
    return os.path.join(state_dir, "state.json")


def events_file(state_dir):
    """Returns the path to the events.jsonl file in the given directory."""
    return os.path.join(state_dir, "events.jsonl")


def init_state(state_dir, initial_equity=1000.0, symbol="BTC-SWAP-USDT"):
    from state_manager import save_state
    state = {
        "version": "1.0.0",
        "initial_equity": initial_equity,
        "equity": initial_equity,
        "peak_equity": initial_equity,
        "symbol": symbol,
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


def validate_jsonl_tail(events_path):
    if not os.path.exists(events_path):
        return
    with open(events_path, 'rb') as f:
        try:
            f.seek(-1, os.SEEK_END)
            last_char = f.read(1)
            if last_char != b'\n':
                raise ValueError(f"JSONL file {events_path} does not end with newline. Truncation suspected.")
        except OSError:
            pass


def load_events(state_dir):
    events_path = events_file(state_dir)
    if not os.path.exists(events_path):
        return []

    validate_jsonl_tail(events_path)

    events = []
    with open(events_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"Corrupted JSONL at {events_path}:{line_num}: {e}")
    return events


def append_events(state_dir, events, strategy_hash):
    if not events:
        return 0, 0

    events_path = events_file(state_dir)
    existing_ids = set()

    if os.path.exists(events_path):
        validate_jsonl_tail(events_path)
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try: existing_ids.add(json.loads(line).get("event_id"))
                    except: pass

    appended = 0
    skipped = 0
    with open(events_path, "a", encoding="utf-8") as f:
        for ev in events:
            ev_id = ev.get("event_id")
            if not ev_id or ev_id in existing_ids:
                skipped += 1
                continue

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