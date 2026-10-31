
import os
import sys
from unittest.mock import Mock, patch

import requests

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

import telegram_notifier as tn


def _response(
    ok=True,
    payload=None,
    status_code=200,
):
    response = Mock()
    response.ok = ok
    response.status_code = status_code
    response.json.return_value = (
        payload
        if payload is not None
        else {"ok": True, "result": {"message_id": 1}}
    )
    return response


def test_send_success_requires_telegram_confirmation(
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    with patch.object(
        tn.requests,
        "post",
        return_value=_response(),
    ) as post:
        assert tn.send_telegram_alert("test") is True
        assert post.call_args.kwargs["timeout"] == (3, 5)

    assert "delivery confirmed" in caplog.text.lower()


def test_missing_secrets_skips_request(monkeypatch):
    monkeypatch.delenv(
        "TELEGRAM_BOT_TOKEN",
        raising=False,
    )
    monkeypatch.delenv(
        "TELEGRAM_CHAT_ID",
        raising=False,
    )

    with patch.object(tn.requests, "post") as post:
        assert tn.send_telegram_alert("test") is False
        post.assert_not_called()


def test_http_error_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    with patch.object(
        tn.requests,
        "post",
        return_value=_response(
            ok=False,
            status_code=500,
        ),
    ):
        assert tn.send_telegram_alert("test") is False


def test_api_ok_false_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    with patch.object(
        tn.requests,
        "post",
        return_value=_response(
            payload={
                "ok": False,
                "description": "denied",
            },
        ),
    ):
        assert tn.send_telegram_alert("test") is False


def test_invalid_json_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    response = _response()
    response.json.side_effect = ValueError("invalid json")

    with patch.object(
        tn.requests,
        "post",
        return_value=response,
    ):
        assert tn.send_telegram_alert("test") is False


def test_timeout_returns_false_without_raising(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    with patch.object(
        tn.requests,
        "post",
        side_effect=requests.exceptions.Timeout(),
    ):
        assert tn.send_telegram_alert("test") is False


def test_baseline_does_not_send_historical_events(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        tn,
        "load_events",
        lambda _path: [
            {"event_id": "old-event"}
        ],
    )

    assert tn.initialize_event_baseline(str(tmp_path)) is True

    state = tn._load_notifier_state(str(tmp_path))

    assert state["sent_event_ids"] == ["old-event"]


def test_event_is_marked_only_after_confirmed_send(
    tmp_path,
    monkeypatch,
    caplog,
):
    state_dir = str(tmp_path)

    assert tn.initialize_event_baseline(state_dir) is True

    monkeypatch.setattr(
        tn,
        "load_events",
        lambda _path: [
            {
                "event_id": "new-event",
                "event_type": "ENTRY",
                "side": "LONG",
                "size": 1,
                "price": 100,
            }
        ],
    )

    monkeypatch.setattr(
        tn,
        "send_telegram_alert",
        lambda _msg: False,
    )

    assert tn.notify_pending_events(state_dir) == 0

    assert (
        "new-event"
        not in tn._load_notifier_state(state_dir)["sent_event_ids"]
    )

    monkeypatch.setattr(
        tn,
        "send_telegram_alert",
        lambda _msg: True,
    )

    assert tn.notify_pending_events(state_dir) == 1

    assert (
        "new-event"
        in tn._load_notifier_state(state_dir)["sent_event_ids"]
    )

    assert "new-event" in caplog.text


def test_run_status_notification_reports_delivery(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        tn,
        "send_telegram_alert",
        lambda _msg: True,
    )

    status = {
        "symbol": "BTC-SWAP-USDT",
        "run_status": "success",
        "run_exit_code": 0,
        "candles_processed_this_run": 1,
        "equity": 1000.0,
        "has_position": False,
        "reconciler_status": "OK",
        "run_finished_at_iso": "2026-10-31T14:00:00+00:00",
    }

    assert tn.notify_run_status(status) is True
    assert "status notification confirmed" in caplog.text.lower()


def test_invalid_status_is_rejected():
    assert tn.notify_run_status(None) is False
