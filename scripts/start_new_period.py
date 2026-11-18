
"""
Safely archive the current PAPER state and begin a new evaluation period.

Safety properties:
- Never discard an open position or pending entry.
- Never reset account risk baselines or circuit-breaker state.
- Never overwrite an existing archive.
- Verify current strategy version AND hash before proceeding.
- Preserve the active state until the archive is complete.
- Use an immutable, uniquely named archive directory.
- Keep the event log and state transition recoverable.

This script is PAPER-only. It does not communicate with an exchange.
"""

import os
import sys
import json
import math
import hashlib
import shutil
import tempfile

from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from compute_hash import get_strategy_metadata


ARCHIVE_ROOT_NAME = "archive_v1"
STATE_FILE_NAME = "state.json"
EVENTS_FILE_NAME = "events.jsonl"


def _utc_now():
    return datetime.now(timezone.utc)


def _period_id():
    return _utc_now().strftime("%Y%m%dT%H%M%S.%fZ")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()

    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as source:
            value = json.load(source)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot read valid JSON from {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise RuntimeError(
            f"Expected a JSON object in {path}"
        )

    return value


def _validate_events(path):
    if not os.path.isfile(path):
        raise RuntimeError("events.jsonl is missing.")

    count = 0

    with open(path, "r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid JSON in events.jsonl at line "
                    f"{line_number}: {exc}"
                ) from exc

            if not isinstance(event, dict):
                raise RuntimeError(
                    f"Invalid event object at line {line_number}"
                )

            count += 1

    return count


def _finite_positive(state, key):
    value = state.get(key)

    if isinstance(value, bool):
        return False

    if not isinstance(value, (int, float)):
        return False

    return math.isfinite(value) and value > 0


def _validate_state(state):
    metadata = get_strategy_metadata()

    if state.get("strategy_hash") != metadata["hash"]:
        raise RuntimeError(
            "Strategy hash mismatch. "
            "State was not changed."
        )

    if state.get("strategy_version") != metadata["version"]:
        raise RuntimeError(
            "Strategy version mismatch. "
            f"State={state.get('strategy_version')!r}, "
            f"Current={metadata['version']!r}. "
            "State was not changed."
        )

    for key in (
        "initial_equity",
        "equity",
        "peak_equity",
    ):
        if not _finite_positive(state, key):
            raise RuntimeError(
                f"Invalid or missing financial field: {key}"
            )

    if state["peak_equity"] < state["equity"]:
        raise RuntimeError(
            "peak_equity is below current equity."
        )

    if state.get("position") is not None:
        raise RuntimeError(
            "Open position detected. "
            "Close and reconcile it before starting a new period."
        )

    if state.get("pending_entry") is not None:
        raise RuntimeError(
            "Pending entry detected. "
            "Reconcile it before starting a new period."
        )

    pending_events = state.get("pending_events", [])

    if not isinstance(pending_events, list):
        raise RuntimeError("pending_events must be a list.")

    if pending_events:
        raise RuntimeError(
            "Pending events detected. "
            "Recover them before starting a new period."
        )


def _copy_exclusive(source_path, destination_path):
    """
    Copy without ever overwriting an existing archive file.
    """

    with open(source_path, "rb") as source:
        with open(destination_path, "xb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())


def _write_atomic(path, content):
    directory = os.path.dirname(path) or "."

    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".period_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())

        os.replace(temp_path, path)

    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


def _json_bytes(value):
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _restore_events(archive_events, active_events):
    """
    Restore the old event log if the state commit point
    has not been reached.
    """

    directory = os.path.dirname(active_events) or "."

    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".events_restore_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "wb") as destination:
            with open(archive_events, "rb") as source:
                shutil.copyfileobj(source, destination)

            destination.flush()
            os.fsync(destination.fileno())

        os.replace(temp_path, active_events)

    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


def start_new_period(state_dir):
    state_dir = os.path.abspath(state_dir)

    if not os.path.isdir(state_dir):
        raise RuntimeError(
            "State directory does not exist."
        )

    state_path = os.path.join(
        state_dir,
        STATE_FILE_NAME,
    )

    events_path = os.path.join(
        state_dir,
        EVENTS_FILE_NAME,
    )

    if not os.path.isfile(state_path):
        raise RuntimeError("state.json is missing.")

    state = _read_json(state_path)

    _validate_state(state)

    event_count = _validate_events(events_path)

    archive_root = os.path.join(
        state_dir,
        ARCHIVE_ROOT_NAME,
    )

    os.makedirs(
        archive_root,
        exist_ok=True,
    )

    period_id = _period_id()

    archive_dir = os.path.join(
        archive_root,
        period_id,
    )

    # Exclusive directory creation: never overwrite an archive.
    os.mkdir(archive_dir)

    archive_state = os.path.join(
        archive_dir,
        STATE_FILE_NAME,
    )

    archive_events = os.path.join(
        archive_dir,
        EVENTS_FILE_NAME,
    )

    try:
        _copy_exclusive(
            state_path,
            archive_state,
        )

        _copy_exclusive(
            events_path,
            archive_events,
        )

        state_hash = _sha256_file(archive_state)
        events_hash = _sha256_file(archive_events)

        manifest = {
            "schema_version": "2.0.0",
            "archive_status": "complete",
            "period_id": period_id,
            "archived_at_utc": _utc_now().isoformat(),
            "source": {
                "state_file": STATE_FILE_NAME,
                "events_file": EVENTS_FILE_NAME,
                "state_sha256": state_hash,
                "events_sha256": events_hash,
                "events_count": event_count,
            },
            "strategy": {
                "version": state["strategy_version"],
                "hash": state["strategy_hash"],
            },
            "financial_snapshot": {
                "initial_equity": state["initial_equity"],
                "equity": state["equity"],
                "peak_equity": state["peak_equity"],
            },
            "position_was_open": False,
            "pending_entry_was_present": False,
            "risk_state_preserved": True,
        }

        manifest_path = os.path.join(
            archive_dir,
            "archive_manifest.json",
        )

        _write_atomic(
            manifest_path,
            _json_bytes(manifest),
        )

    except Exception:
        # Preserve any partial archive as forensic evidence.
        raise

    # New evaluation period:
    # preserve all account/risk fields.
    new_state = dict(state)

    started_at = _utc_now().isoformat()

    new_state["period_start_utc"] = started_at
    new_state["period_start_equity"] = state["equity"]
    new_state["period_id"] = period_id

    # Do NOT reset:
    # - initial_equity
    # - peak_equity
    # - daily_loss
    # - weekly_loss
    # - loss_day_key
    # - loss_week_key
    # - circuit_breaker_active
    # - halt_trading
    # - last_processed_candle_ts

    state_committed = False

    try:
        # Prepare both files before replacing either one.
        state_temp = os.path.join(
            state_dir,
            ".state_period_new.tmp",
        )

        events_temp = os.path.join(
            state_dir,
            ".events_period_new.tmp",
        )

        with open(state_temp, "xb") as destination:
            destination.write(_json_bytes(new_state))
            destination.flush()
            os.fsync(destination.fileno())

        with open(events_temp, "xb") as destination:
            destination.flush()
            os.fsync(destination.fileno())

        # Replace events first. State is the commit marker.
        os.replace(
            events_temp,
            events_path,
        )

        os.replace(
            state_temp,
            state_path,
        )

        state_committed = True

    except Exception as exc:
        for temp_path in (
            os.path.join(state_dir, ".state_period_new.tmp"),
            os.path.join(state_dir, ".events_period_new.tmp"),
        ):
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        if not state_committed:
            try:
                _restore_events(
                    archive_events,
                    events_path,
                )
            except Exception as restore_error:
                raise RuntimeError(
                    "Period transition failed and event-log "
                    "rollback failed. Preserve archive at "
                    f"{archive_dir}. "
                    f"Rollback error: {restore_error}"
                ) from exc

        raise

    print("=" * 65)
    print("NEW PAPER EVALUATION PERIOD STARTED")
    print("=" * 65)
    print(f"Period ID       : {period_id}")
    print(f"Strategy        : {state['strategy_version']}")
    print(f"Strategy hash   : {state['strategy_hash']}")
    print(f"Period baseline : {state['equity']}")
    print(f"Initial equity  : {state['initial_equity']}")
    print(f"Peak equity     : {state['peak_equity']}")
    print(f"Archived events : {event_count}")
    print(f"Archive         : {archive_dir}")
    print("Risk state      : PRESERVED")
    print("=" * 65)

    return {
        "period_id": period_id,
        "archive_dir": archive_dir,
        "events_count": event_count,
        "period_start_equity": state["equity"],
    }


def main():
    state_dir = os.environ.get(
        "STATE_DIR",
        "./paper_state_dir",
    )

    start_new_period(state_dir)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PERIOD START ABORTED: {exc}")
        sys.exit(1)
