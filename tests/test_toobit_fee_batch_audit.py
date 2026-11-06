
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import toobit_fee_batch_audit as audit


def response(payload, status=200, error=None):
    return {
        "http_status": status,
        "response_json": payload,
        "error": error,
    }


def fee_payload(**overrides):
    values = {
        "openMakerFee": "0.0002",
        "openTakerFee": "0.0006",
        "closeMakerFee": "0.0002",
        "closeTakerFee": "0.0006",
    }

    values.update(overrides)
    return values


def test_load_symbols_rejects_duplicate(tmp_path):
    path = tmp_path / "symbols.json"

    path.write_text(
        '{"symbols":["BTC-SWAP-USDT","BTC-SWAP-USDT"]}'
    )

    with pytest.raises(ValueError, match="duplicate"):
        audit.load_symbols(path)


def test_audit_symbol_accepts_complete_sane_fees(monkeypatch):
    monkeypatch.setattr(
        audit,
        "_private_get",
        lambda *a, **k: response(fee_payload()),
    )

    result = audit.audit_symbol(
        "key",
        "secret",
        "BTC-SWAP-USDT",
    )

    assert result["ok"] is True
    assert result["round_trip_taker_rate"] == pytest.approx(0.0012)
    assert result["blockers"] == []


def test_audit_symbol_fails_when_field_missing(monkeypatch):
    payload = fee_payload()
    payload.pop("closeMakerFee")

    monkeypatch.setattr(
        audit,
        "_private_get",
        lambda *a, **k: response(payload),
    )

    result = audit.audit_symbol(
        "key",
        "secret",
        "BTC-SWAP-USDT",
    )

    assert result["ok"] is False
    assert "closeMakerFee" in result["missing_fields"]


def test_audit_symbol_fails_when_fee_sanity_fails(monkeypatch):
    monkeypatch.setattr(
        audit,
        "_private_get",
        lambda *a, **k: response(
            fee_payload(openTakerFee="0.0001")
        ),
    )

    result = audit.audit_symbol(
        "key",
        "secret",
        "BTC-SWAP-USDT",
    )

    assert result["ok"] is False
    assert "fee_sanity_check_failed" in result["blockers"]


def test_build_report_stops_when_account_preflight_fails(monkeypatch):
    monkeypatch.setattr(
        audit,
        "_private_get",
        lambda *a, **k: response([], status=401),
    )

    report = audit.build_report(
        "key",
        "secret",
        ["BTC-SWAP-USDT"],
    )

    assert report["ok"] is False
    assert report["account_preflight_ok"] is False
    assert report["symbols_checked"] == 0
    assert report["orders_submitted"] == 0


def test_build_report_checks_every_symbol_and_never_enables_live(monkeypatch):
    calls = []

    def fake_get(key, secret, endpoint, params=None, timeout=15):
        calls.append((endpoint, params or {}))

        if endpoint == audit.BALANCE_PATH:
            return response([])

        return response(fee_payload())

    monkeypatch.setattr(
        audit,
        "_private_get",
        fake_get,
    )

    symbols = [
        "BTC-SWAP-USDT",
        "ETH-SWAP-USDT",
    ]

    report = audit.build_report(
        "key",
        "secret",
        symbols,
    )

    assert report["ok"] is True
    assert report["symbols_checked"] == 2
    assert report["symbols_passed"] == 2
    assert len(calls) == 3
    assert report["live_execution_enabled"] is False
    assert report["orders_submitted"] == 0


def test_transport_error_fails_symbol_closed(monkeypatch):
    monkeypatch.setattr(
        audit,
        "_private_get",
        lambda *a, **k: response(
            None,
            status=None,
            error="Timeout",
        ),
    )

    result = audit.audit_symbol(
        "key",
        "secret",
        "BTC-SWAP-USDT",
    )

    assert result["ok"] is False
    assert result["blockers"]
