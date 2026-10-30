
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

from compute_hash import compute_strategy_hash, get_strategy_metadata
from state_manager import load_state, save_state


SOURCE_VERSION = "1.0.1"
TARGET_VERSION = "1.1.0"

SOURCE_HASH = (
    "sha256:7d608627418f8c9a9f7d157d03d0fdc5e25add19cc35f0cc3ee1cc024c347d90"
)

TARGET_HASH = (
    "sha256:0de5d79fa280590cb4f316200ed31c7e110c6d8dee448ca74650bfef95bb07e7"
)

EXPECTED_EVENT_COUNT = 95
EXPECTED_EVENT_TYPE = "COOLDOWN_SKIP"

EXPECTED_STATE = {
    "symbol": "BTC-SWAP-USDT",
    "initial_equity": 971.8365982771752,
    "equity": 984.6741532799955,
    "peak_equity": 984.6741532799955,
    "last_processed_candle_ts": 1793376000000,
}

BACKUP_NAME = "state.json.pre_migration_1.0.1_to_1.1.0.bak"
MANIFEST_NAME = "migration_1.0.1_to_1.1.0.json"


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as f:
        return _sha256_bytes(f.read())


def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".migration_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)

    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _restore_backup(backup_path, state_path):
    directory = os.path.dirname(state_path) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".state_restore_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "wb") as dst:
            with open(backup_path, "rb") as src:
                shutil.copyfileobj(src, dst)

            dst.flush()
            os.fsync(dst.fileno())

        os.replace(temp_path, state_path)

    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _verify_target_strategy():
    metadata = get_strategy_metadata()

    if metadata["version"] != TARGET_VERSION:
        raise RuntimeError(
            f"Wrong target version: {metadata['version']}"
        )

    if metadata["hash"] != TARGET_HASH:
        raise RuntimeError(
            "Current strategy hash does not match approved target."
        )

    if compute_strategy_hash() != TARGET_HASH:
        raise RuntimeError("Strategy hash verification failed.")


def _verify_state(state):
    if state.get("strategy_version") != SOURCE_VERSION:
        raise RuntimeError("Unexpected source strategy version.")

    if state.get("strategy_hash") != SOURCE_HASH:
        raise RuntimeError("Unexpected source strategy hash.")

    if state.get("symbol") != EXPECTED_STATE["symbol"]:
        raise RuntimeError("Unexpected State symbol.")

    for key in (
        "initial_equity",
        "equity",
        "peak_equity",
    ):
        actual = state.get(key)
        expected = EXPECTED_STATE[key]

        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isclose(
                actual,
                expected,
                rel_tol=0,
                abs_tol=1e-10,
            )
        ):
            raise RuntimeError(
                f"Unexpected State value: {key}={actual!r}"
            )

    if (
        state.get("last_processed_candle_ts")
        != EXPECTED_STATE["last_processed_candle_ts"]
    ):
        raise RuntimeError("Unexpected last processed candle.")

    if state.get("position") is not None:
        raise RuntimeError(
            "Open position detected. Migration refused."
        )

    if state.get("pending_entry") is not None:
        raise RuntimeError(
            "Pending entry detected. Migration refused."
        )

    if state.get("pending_events") not in (None, []):
        raise RuntimeError(
            "Pending events detected. Migration refused."
        )

    if state.get("circuit_breaker_active") is not False:
        raise RuntimeError(
            "Circuit breaker state is not the expected value."
        )

    if state.get("halt_trading", False):
        raise RuntimeError(
            "Trading halt detected. Migration refused."
        )


def _audit_events(events_path):
    if not os.path.isfile(events_path):
        raise RuntimeError("events.jsonl not found.")

    with open(events_path, "rb") as f:
        raw = f.read()

    if not raw:
        raise RuntimeError("events.jsonl is empty.")

    previous_seq = 0
    event_ids = set()
    count = 0

    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError("events.jsonl is not valid UTF-8.") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON at events line {line_number}"
            ) from exc

        if not isinstance(event, dict):
            raise RuntimeError(
                f"Invalid event object at line {line_number}"
            )

        if event.get("strategy_hash") != SOURCE_HASH:
            raise RuntimeError(
                f"Unexpected event hash at line {line_number}"
            )

        if event.get("strategy_version") != SOURCE_VERSION:
            raise RuntimeError(
                f"Unexpected event version at line {line_number}"
            )

        if event.get("event_type") != EXPECTED_EVENT_TYPE:
            raise RuntimeError(
                f"Unexpected event type at line {line_number}"
            )

        seq = event.get("seq")

        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or seq <= previous_seq
        ):
            raise RuntimeError(
                f"Invalid event sequence at line {line_number}"
            )

        previous_seq = seq

        event_id = event.get("event_id")

        if not isinstance(event_id, str) or not event_id:
            raise RuntimeError(
                f"Missing event ID at line {line_number}"
            )

        if event_id in event_ids:
            raise RuntimeError(
                f"Duplicate event ID at line {line_number}"
            )

        event_ids.add(event_id)
        count += 1

    if count != EXPECTED_EVENT_COUNT:
        raise RuntimeError(
            f"Unexpected event count: {count}; "
            f"expected {EXPECTED_EVENT_COUNT}"
        )

    return count, _sha256_bytes(raw)


def migrate(state_dir):
    state_dir = os.path.abspath(state_dir)

    if not os.path.isdir(state_dir):
        raise RuntimeError("State directory does not exist.")

    state_path = os.path.join(state_dir, "state.json")
    events_path = os.path.join(state_dir, "events.jsonl")
    backup_path = os.path.join(state_dir, BACKUP_NAME)
    manifest_path = os.path.join(state_dir, MANIFEST_NAME)

    if os.path.exists(backup_path):
        raise RuntimeError(
            "Migration backup already exists. Refusing overwrite."
        )

    if os.path.exists(manifest_path):
        raise RuntimeError(
            "Migration manifest already exists. Refusing repeat."
        )

    _verify_target_strategy()

    state = load_state(state_path, strict=False)
    _verify_state(state)

    event_count, events_hash_before = _audit_events(events_path)

    original_state = dict(state)

    # Create a permanent backup before changing anything.
    shutil.copy2(state_path, backup_path)

    if _sha256_file(backup_path) != _sha256_file(state_path):
        raise RuntimeError("Backup verification failed.")

    state_written = False

    try:
        migrated_state = dict(state)

        # Only these two metadata fields are changed.
        migrated_state["strategy_hash"] = TARGET_HASH
        migrated_state["strategy_version"] = TARGET_VERSION

        save_state(state_path, migrated_state)
        state_written = True

        verified = load_state(state_path, strict=True)

        expected = dict(original_state)
        expected["strategy_hash"] = TARGET_HASH
        expected["strategy_version"] = TARGET_VERSION

        if verified != expected:
            raise RuntimeError(
                "Post-migration State comparison failed."
            )

        events_hash_after = _sha256_file(events_path)

        if events_hash_after != events_hash_before:
            raise RuntimeError(
                "Event history changed during migration."
            )

        manifest = {
            "migration_id": "paper_v1_0_1_to_v1_1_0",
            "migration_status": "success",
            "migrated_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "source": {
                "version": SOURCE_VERSION,
                "hash": SOURCE_HASH,
            },
            "target": {
                "version": TARGET_VERSION,
                "hash": TARGET_HASH,
            },
            "state_file": "state.json",
            "backup_file": BACKUP_NAME,
            "events_file": "events.jsonl",
            "events_count": event_count,
            "events_sha256": events_hash_before,
            "events_rewritten": False,
            "state_values_preserved": True,
            "position_was_open": False,
            "pending_entry_was_present": False,
        }

        _atomic_write_json(manifest_path, manifest)

    except Exception:
        if state_written:
            try:
                _restore_backup(backup_path, state_path)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Migration failed and automatic rollback failed. "
                    f"Preserve backup: {backup_path}. "
                    f"Rollback error: {rollback_error}"
                )

        raise

    print("=" * 65)
    print("PAPER STATE MIGRATION SUCCESS")
    print("=" * 65)
    print(f"Source version : {SOURCE_VERSION}")
    print(f"Target version : {TARGET_VERSION}")
    print(f"Events audited : {event_count}")
    print("Events changed : NO")
    print("Equity changed : NO")
    print("Position       : NONE")
    print(f"Backup         : {backup_path}")
    print(f"Manifest       : {manifest_path}")
    print("=" * 65)

    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-dir",
        default="./paper_state_dir",
    )

    args = parser.parse_args()

    try:
        migrate(args.state_dir)
    except Exception as exc:
        print(f"MIGRATION ABORTED: {exc}")
        sys.exit(1)
