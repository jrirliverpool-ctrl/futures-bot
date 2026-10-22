"""Fail-safe Telegram notifications for ApexQuant MOCK_LIVE.

Secrets:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Event delivery is at-least-once: a crash after Telegram accepts a message
but before the local marker is saved can cause a duplicate.
"""
import json
import logging
import os
import tempfile

import requests

from paper_state_manager import load_events

logger = logging.getLogger("ApexQuant.Telegram")

STATE_FILENAME = "telegram_notifier_state.json"
STATE_SCHEMA = "1.0.0"

def _state_path(state_dir):
    return os.path.join(state_dir, STATE_FILENAME)

def _credentials_configured():
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN")
        and os.environ.get("TELEGRAM_CHAT_ID")
    )

def send_telegram_alert(message: str) -> bool:
    """Return True only when Telegram API confirms ok=true."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("Telegram skipped: credentials are not configured.")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": str(message)[:4000],
                "disable_web_page_preview": True,
            },
            timeout=(3, 5),
        )
        if not response.ok:
            logger.warning(
                "Telegram delivery failed: HTTP %s",
                response.status_code,
            )
            return False

        try:
            payload = response.json()
        except ValueError:
            logger.warning("Telegram returned invalid JSON.")
            return False

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            logger.warning("Telegram did not confirm message delivery.")
            return False

        return True
    except requests.RequestException as exc:
        # Never log exception text: it may contain a URL with the bot token.
        logger.warning("Telegram request failed: %s", type(exc).__name__)
        return False
    except Exception as exc:
        logger.warning("Telegram notifier failed: %s", type(exc).__name__)
        return False

def _write_json_atomic(path, payload):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".telegram_state_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                payload, f, sort_keys=True, indent=2,
                ensure_ascii=True, allow_nan=False,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise

def _load_notifier_state(state_dir):
    path = _state_path(state_dir)
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    if (
        not isinstance(state, dict)
        or state.get("schema_version") != STATE_SCHEMA
        or not isinstance(state.get("sent_event_ids"), list)
        or not all(isinstance(x, str) for x in state["sent_event_ids"])
    ):
        raise ValueError("Invalid Telegram notifier state schema")

    return state

def initialize_event_baseline(state_dir) -> bool:
    """Mark pre-existing events as historical; do not send them on first setup.

    Call once near startup, before processing new candles.
    Existing marker files are never overwritten.
    """
    path = _state_path(state_dir)
    try:
        if os.path.exists(path):
            _load_notifier_state(state_dir)
            return True

        existing_ids = {
            event["event_id"]
            for event in load_events(state_dir)
            if isinstance(event, dict)
            and isinstance(event.get("event_id"), str)
        }
        _write_json_atomic(path, {
            "schema_version": STATE_SCHEMA,
            "sent_event_ids": sorted(existing_ids),
        })
        return True
    except Exception as exc:
        logger.warning(
            "Could not initialize Telegram event baseline: %s",
            type(exc).__name__,
        )
        return False

def _format_event(event):
    event_type = event.get("event_type") or "UNKNOWN"
    lines = [
        "ApexQuant | MOCK_LIVE event",
        f"Event: {event_type}",
        f"Symbol: {os.environ.get('SYMBOL', 'BTC-SWAP-USDT')}",
        f"Side: {event.get('side') or 'N/A'}",
        f"Size: {event.get('size') if event.get('size') is not None else 'N/A'}",
        f"Price: {event.get('price') if event.get('price') is not None else 'N/A'}",
        f"PnL USD: {event.get('pnl_usd') if event.get('pnl_usd') is not None else 'N/A'}",
        f"Time: {event.get('timestamp_iso') or event.get('candle_ts_ms') or 'N/A'}",
        f"Event ID: {event.get('event_id') or 'N/A'}",
    ]
    if event.get("reason"):
        lines.append(f"Reason: {str(event['reason'])[:300]}")
    return "\n".join(lines)

def notify_pending_events(state_dir) -> int:
    """Send events absent from the local sent-ID registry.

    Returns the count confirmed by Telegram. Failures remain retryable.
    """
    try:
        notifier_state = _load_notifier_state(state_dir)
        if notifier_state is None:
            logger.warning(
                "Telegram event notifications paused: baseline not initialized."
            )
            return 0

        sent_ids = set(notifier_state["sent_event_ids"])
        events = load_events(state_dir)
        sent_count = 0

        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or event_id in sent_ids:
                continue

            if not send_telegram_alert(_format_event(event)):
                break

            sent_ids.add(event_id)
            notifier_state["sent_event_ids"] = sorted(sent_ids)
            try:
                _write_json_atomic(_state_path(state_dir), notifier_state)
            except Exception as exc:
                logger.warning(
                    "Could not persist Telegram sent marker: %s",
                    type(exc).__name__,
                )
                break
            sent_count += 1

        return sent_count
    except Exception as exc:
        logger.warning(
            "Telegram event processing failed: %s",
            type(exc).__name__,
        )
        return 0

def notify_run_status(status: dict) -> bool:
    """Send one concise final status notification; never raise."""
    try:
        symbol = status.get("symbol", "BTC-SWAP-USDT")
        run_status = status.get("run_status", "unknown")
        exit_code = status.get("run_exit_code", "unknown")
        lines = [
            f"ApexQuant | MOCK_LIVE {str(run_status).upper()}",
            f"Symbol: {symbol}",
            f"Exit code: {exit_code}",
            f"Candles processed: {status.get('candles_processed_this_run', 0)}",
            f"Equity: {status.get('equity') if status.get('equity') is not None else 'N/A'}",
            f"Position open: {status.get('has_position') if status.get('has_position') is not None else 'N/A'}",
            f"Reconciler: {status.get('reconciler_status') or 'N/A'}",
            f"Finished: {status.get('run_finished_at_iso') or 'N/A'}",
        ]
        if status.get("error_message"):
            lines.append(f"Error: {str(status['error_message'])[:700]}")
        return send_telegram_alert("\n".join(lines))
    except Exception as exc:
        logger.warning(
            "Telegram status notification failed: %s",
            type(exc).__name__,
        )
        return False