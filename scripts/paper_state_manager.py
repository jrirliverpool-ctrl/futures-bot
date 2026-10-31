"""paper_state_manager.py

Strict JSONL handling for trades and events.

v3.10.0
- Atomic event-log replacement.
- Idempotent event recovery.
- Fail-closed handling of corrupted JSONL.
- Preserve existing sequence numbers and event history.
"""

import json
import os
import logging
import hashlib
import shutil
import stat
import tempfile

from datetime import datetime, timezone

logger = logging.getLogger("PaperStateManager")


def state_file(state_dir):
    return os.path.join(state_dir, "state.json")


def events_file(state_dir):
    return os.path.join(state_dir, "events.jsonl")


def _fingerprint(event, strategy_hash):
    payload = json.dumps(
        event,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    )

    return hashlib.sha256(
        f"{payload}{strategy_hash}".encode("utf-8")
    ).hexdigest()[:16]


def init_state(
    state_dir,
    initial_equity=1000.0,
    symbol="BTC-SWAP-USDT",
):
    from state_manager import (
        save_state,
        default_state,
        load_state,
    )

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
    """
    Load JSONL events.

    Any malformed line is a hard error.
    Automatic repair is deliberately disabled.
    """

    path = events_file(state_dir)

    if not os.path.exists(path):
        return []

    events = []

    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):

            line = line.strip()

            if not line:
                continue

            try:
                event = json.loads(line)

            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Corrupted events file {path} "
                    f"at line {line_num}: {exc}. "
                    "Manual repair required."
                ) from exc

            if not isinstance(event, dict):
                raise ValueError(
                    f"Invalid event object at line {line_num}."
                )

            events.append(event)

    return events


def _fsync_directory(directory):
    """
    Best-effort directory fsync after atomic replacement.
    """

    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(directory, flags)

        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    except OSError:
        pass


def append_events(
    state_dir,
    events,
    strategy_hash=None,
):
    """
    Atomically append events to events.jsonl.

    Guarantees:
    - Existing event history is preserved.
    - Duplicate events are skipped.
    - Sequence numbers remain monotonic.
    - A failed temporary write does not damage the old log.
    - A crash after replacement but before Outbox clearing
      is recoverable through event_id deduplication.

    Existing corrupted files are never silently repaired.
    """

    if not events:
        return 0, 0

    from compute_hash import (
        compute_strategy_hash,
        get_strategy_metadata,
    )

    if strategy_hash is None:
        strategy_hash = compute_strategy_hash()

    metadata = get_strategy_metadata()

    os.makedirs(state_dir, exist_ok=True)

    path = events_file(state_dir)

    existing_file = os.path.exists(path)

    existing_ids = set()
    existing_max_seq = 0

    # ---------------------------------------------------------
    # 1. Validate existing history and collect identifiers.
    # ---------------------------------------------------------

    if existing_file:

        with open(path, "r", encoding="utf-8") as f:

            for line_num, line in enumerate(f, 1):

                line = line.strip()

                if not line:
                    continue

                try:
                    event = json.loads(line)

                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Corrupted events file at line "
                        f"{line_num}: {exc}. "
                        "Refusing to append."
                    ) from exc

                if not isinstance(event, dict):
                    raise ValueError(
                        f"Invalid event object at line {line_num}."
                    )

                event_id = event.get("event_id")

                if isinstance(event_id, str):
                    existing_ids.add(event_id)

                seq = event.get("seq")

                if (
                    isinstance(seq, int)
                    and not isinstance(seq, bool)
                    and seq > existing_max_seq
                ):
                    existing_max_seq = seq

    # ---------------------------------------------------------
    # 2. Prepare all records before touching the existing log.
    # ---------------------------------------------------------

    prepared_lines = []

    appended = 0
    skipped = 0

    next_seq = existing_max_seq + 1

    for original_event in events:

        if not isinstance(original_event, dict):
            raise ValueError(
                "Each event must be a dictionary."
            )

        event = original_event.copy()

        if "type" in event:
            event["event_type"] = event.pop("type")

        if "timestamp" in event:

            timestamp = event.pop("timestamp")

            event["candle_ts_ms"] = timestamp

            event["timestamp_iso"] = (
                datetime.fromtimestamp(
                    timestamp / 1000.0,
                    tz=timezone.utc,
                ).isoformat()
            )

        event["strategy_hash"] = strategy_hash

        event["strategy_version"] = metadata["version"]

        event_id = _fingerprint(
            event,
            strategy_hash,
        )

        if event_id in existing_ids:
            skipped += 1
            continue

        event["event_id"] = event_id
        event["seq"] = next_seq

        next_seq += 1

        # Validate serialization before modifying the log.
        serialized = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"

        prepared_lines.append(
            serialized.encode("utf-8")
        )

        existing_ids.add(event_id)
        appended += 1

    if not prepared_lines:
        return 0, skipped

    # ---------------------------------------------------------
    # 3. Write a complete replacement in the same directory.
    # ---------------------------------------------------------

    fd, tmp_path = tempfile.mkstemp(
        dir=state_dir,
        prefix=".events_",
        suffix=".tmp",
    )

    try:

        with os.fdopen(fd, "wb") as target:

            if existing_file:

                with open(path, "rb") as source:

                    shutil.copyfileobj(
                        source,
                        target,
                    )

                    source.seek(0, os.SEEK_END)

                    size = source.tell()

                    if size > 0:

                        source.seek(-1, os.SEEK_END)

                        last_byte = source.read(1)

                        # A valid JSON object without a final
                        # newline must be separated from new data.
                        if last_byte != b"\n":
                            target.write(b"\n")

            for line in prepared_lines:
                target.write(line)

            target.flush()
            os.fsync(target.fileno())

        # Preserve existing file permissions.
        if existing_file:

            old_mode = stat.S_IMODE(
                os.stat(path).st_mode
            )

            os.chmod(
                tmp_path,
                old_mode,
            )

        # Atomic replacement on the same filesystem.
        os.replace(
            tmp_path,
            path,
        )

        _fsync_directory(state_dir)

    except Exception:

        if os.path.exists(tmp_path):

            try:
                os.remove(tmp_path)

            except OSError:
                pass

        raise

    return appended, skipped


def events_count(state_dir):

    path = events_file(state_dir)

    if not os.path.exists(path):
        return 0

    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def max_seq(events):

    if not events:
        return 0

    seqs = []

    for event in events:

        value = event.get("seq", 0)

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
        ):
            raise ValueError(
                f"Invalid event seq (must be int): {value!r}"
            )

        if value < 0:
            raise ValueError(
                f"Invalid event seq (must be >= 0): {value!r}"
            )

        seqs.append(value)

    return max(seqs, default=0