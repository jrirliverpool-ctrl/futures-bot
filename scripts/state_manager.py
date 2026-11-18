
"""state_manager.py — Atomic State validation with strict strategy identity."""

import csv
import hashlib
import json
import logging
import math
import os
import tempfile

from datetime import datetime, timezone

from compute_hash import (
    compute_strategy_hash,
    get_strategy_metadata,
)

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = "1.0.0"

TRADE_LOG_COLUMNS = [
    "event_id",
    "event_type",
    "event_seq",
    "timestamp_iso",
    "symbol",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "pnl_usd",
    "commission_usd",
    "reason",
    "equity",
    "strategy_version",
    "strategy_hash",
]


def _get_state_path(path):
    if path.endswith(".json"):
        return path

    return os.path.join(path, "state.json")


def default_state(initial_equity=1000.0):
    if isinstance(initial_equity, bool):
        raise ValueError(
            "initial_equity must be a number, not bool"
        )

    if not isinstance(initial_equity, (int, float)):
        raise ValueError(
            "initial_equity must be a number"
        )

    if not math.isfinite(initial_equity):
        raise ValueError(
            "initial_equity must be finite"
        )

    if initial_equity <= 0:
        raise ValueError(
            "initial_equity must be > 0"
        )

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


def _check_finite_values(obj, path):
    if isinstance(obj, dict):
        for key, value in obj.items():
            _check_finite_values(
                value,
                f"{path}.{key}",
            )

    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _check_finite_values(
                value,
                f"{path}[{index}]",
            )

    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(
                f"Invalid numeric value at {path}: {obj}"
            )


def validate_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError(
            "state must be a dict"
        )

    if state.get("version") != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"version must be {STATE_SCHEMA_VERSION}, "
            f"got {state.get('version')!r}"
        )

    _check_finite_values(state, "state")

    for key in (
        "initial_equity",
        "equity",
        "peak_equity",
    ):
        if key not in state:
            raise ValueError(
                f"Missing required key: {key}"
            )

        value = state[key]

        if isinstance(value, bool) or not isinstance(
            value,
            (int, float),
        ):
            raise ValueError(
                f"{key} must be a number"
            )

        if not math.isfinite(value):
            raise ValueError(
                f"{key} must be finite"
            )

        if value < 0:
            raise ValueError(
                f"{key} must be >= 0"
            )

    if (
        state.get("position") is not None
        and not isinstance(state["position"], dict)
    ):
        raise ValueError(
            "position must be None or dict"
        )

    if (
        state.get("pending_entry") is not None
        and not isinstance(state["pending_entry"], dict)
    ):
        raise ValueError(
            "pending_entry must be None or dict"
        )

    last_processed = state.get(
        "last_processed_candle_ts"
    )

    if last_processed is not None:
        if (
            isinstance(last_processed, bool)
            or not isinstance(last_processed, int)
        ):
            raise ValueError(
                "last_processed_candle_ts must be None or int"
            )

    cooldown = state.get(
        "cooldown_until_index"
    )

    if cooldown is not None:
        if (
            isinstance(cooldown, bool)
            or not isinstance(cooldown, int)
        ):
            raise ValueError(
                "cooldown_until_index must be None or int"
            )


def save_state(path, state):
    validate_state(state)

    target = _get_state_path(path)
    state_dir = os.path.dirname(target)

    if state_dir:
        os.makedirs(
            state_dir,
            exist_ok=True,
        )

    fd, temp_path = tempfile.mkstemp(
        dir=state_dir or ".",
        prefix=".state_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as destination:
            json.dump(
                state,
                destination,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )

            destination.flush()
            os.fsync(destination.fileno())

        os.replace(
            temp_path,
            target,
        )

    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        raise


def load_state(path, strict=True):
    target = _get_state_path(path)

    if not os.path.exists(target):
        raise FileNotFoundError(
            f"State not found at {target}"
        )

    with open(
        target,
        "r",
        encoding="utf-8",
    ) as source:
        state = json.load(source)

    validate_state(state)

    if strict:
        try:
            metadata = get_strategy_metadata()
        except Exception as exc:
            raise ValueError(
                "Could not compute current strategy metadata: "
                f"{exc}"
            ) from exc

        current_hash = metadata["hash"]
        current_version = metadata["version"]

        saved_hash = state.get("strategy_hash")
        saved_version = state.get("strategy_version")

        if saved_hash is None:
            raise ValueError(
                "State missing strategy_hash. "
                f"Expected: {current_hash}."
            )

        if saved_hash != current_hash:
            raise ValueError(
                "Strategy hash mismatch! "
                f"Saved: {saved_hash}, "
                f"Current: {current_hash}."
            )

        if saved_version is None:
            raise ValueError(
                "State missing strategy_version. "
                f"Expected: {current_version}."
            )

        if saved_version != current_version:
            raise ValueError(
                "Strategy version mismatch! "
                f"Saved: {saved_version}, "
                f"Current: {current_version}."
            )

    return state


def delete_state(path):
    target = _get_state_path(path)

    if os.path.exists(target):
        os.remove(target)

    state_dir = os.path.dirname(target)

    if state_dir and os.path.exists(state_dir):
        for filename in os.listdir(state_dir):
            if (
                filename.startswith("state.json.bak")
                or filename.startswith(".state_")
            ):
                try:
                    os.remove(
                        os.path.join(
                            state_dir,
                            filename,
                        )
                    )
                except OSError:
                    pass


def state_exists(path):
    return os.path.exists(
        _get_state_path(path)
    )


def load_or_init_state(
    path,
    initial_equity=1000.0,
    symbol="UNKNOWN",
):
    if state_exists(path):
        return load_state(
            path,
            strict=True,
        )

    state = default_state(
        initial_equity
    )

    state["symbol"] = symbol

    save_state(
        path,
        state,
    )

    return state


def trade_log_exists(csv_path):
    return os.path.exists(csv_path)


def delete_trade_log(csv_path):
    if os.path.exists(csv_path):
        os.remove(csv_path)


def _event_identity(event):
    payload = {
        key: value
        for key, value in event.items()
        if key not in ("seq", "event_id")
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()[:16]


def _validate_event_for_csv(event):
    if not isinstance(event, dict):
        raise ValueError(
            "event must be a dict"
        )

    timestamp = event.get("timestamp")

    if timestamp is not None:
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
        ):
            raise ValueError(
                f"Invalid timestamp type: {type(timestamp).__name__}"
            )

        if not math.isfinite(timestamp):
            raise ValueError(
                "timestamp must be finite"
            )

        try:
            datetime.fromtimestamp(
                float(timestamp) / 1000.0,
                tz=timezone.utc,
            )

        except (
            OverflowError,
            OSError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"Timestamp out of range: {timestamp!r} ({exc})"
            ) from exc


def _read_trade_log_rows(csv_path):
    if not os.path.exists(csv_path):
        return []

    with open(
        csv_path,
        "r",
        encoding="utf-8",
        newline="",
    ) as source:
        reader = csv.DictReader(source)
        return list(reader)


def _last_valid_seq(rows):
    last = 0

    for row in rows:
        value = row.get("event_seq")

        if value in (None, ""):
            continue

        try:
            number = int(value)
        except (TypeError, ValueError):
            continue

        if number > last:
            last = number

    return last


def append_trade_events(
    csv_path,
    events,
    strategy_metadata=None,
):
    if not events:
        return 0, 0

    if strategy_metadata is None:
        strategy_metadata = get_strategy_metadata()

    for event in events:
        _validate_event_for_csv(event)

    try:
        existing_rows = _read_trade_log_rows(
            csv_path
        )
    except Exception as exc:
        raise ValueError(
            f"Could not read existing trade log "
            f"{csv_path}: {exc}"
        ) from exc

    existing_ids = set()

    for row in existing_rows:
        event_id = row.get("event_id")

        if event_id:
            existing_ids.add(event_id)

    start_seq = _last_valid_seq(existing_rows) + 1
    file_exists = os.path.exists(csv_path)

    os.makedirs(
        os.path.dirname(csv_path) or ".",
        exist_ok=True,
    )

    appended = 0
    skipped = 0

    with open(
        csv_path,
        "a",
        encoding="utf-8",
        newline="",
    ) as destination:
        writer = csv.DictWriter(
            destination,
            fieldnames=TRADE_LOG_COLUMNS,
            extrasaction="ignore",
        )

        if not file_exists:
            writer.writeheader()

        for event in events:
            event_id = _event_identity(event)

            if event_id in existing_ids:
                skipped += 1
                continue

            row = {
                column: ""
                for column in TRADE_LOG_COLUMNS
            }

            row["event_id"] = event_id
            row["event_type"] = event.get(
                "type",
                "UNKNOWN",
            )
            row["event_seq"] = start_seq + appended

            timestamp = event.get("timestamp")

            if timestamp is not None:
                row["timestamp_iso"] = datetime.fromtimestamp(
                    float(timestamp) / 1000.0,
                    tz=timezone.utc,
                ).isoformat()

            row["symbol"] = event.get(
                "symbol",
                "UNKNOWN",
            )

            row["side"] = event.get("side", "")

            row["qty"] = event.get(
                "size",
                event.get("qty", ""),
            )

            row["entry_price"] = event.get(
                "entry_price",
                event.get("price", ""),
            )

            row["exit_price"] = event.get(
                "exit_price_adj",
                event.get("price", ""),
            )

            row["pnl_usd"] = event.get(
                "pnl_usd",
                "",
            )

            row["commission_usd"] = event.get(
                "commission_usd",
                "",
            )

            row["reason"] = event.get(
                "reason",
                "",
            )

            row["equity"] = event.get(
                "equity",
                "",
            )

            row["strategy_version"] = strategy_metadata.get(
                "version",
                "",
            )

            row["strategy_hash"] = strategy_metadata.get(
                "hash",
                "",
            )

            writer.writerow(row)

            existing_ids.add(event_id)
            appended += 1

    return appended, skipped


def load_trade_log(
    csv_path,
    validate_header=True,
):
    if not os.path.exists(csv_path):
        return []

    if validate_header:
        with open(
            csv_path,
            "r",
            encoding="utf-8",
            newline="",
        ) as source:
            header_line = source.readline().strip()

        header = (
            header_line.split(",")
            if header_line
            else []
        )

        if header != TRADE_LOG_COLUMNS:
            raise ValueError(
                "CSV header mismatch. "
                f"Expected {TRADE_LOG_COLUMNS}, "
                f"got {header}"
            )

    return _read_trade_log_rows(csv_path)
