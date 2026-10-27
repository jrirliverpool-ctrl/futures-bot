"""tests/test_probe_all_symbols.py — Unit tests for probe_all_symbols.

All network calls are mocked. Tests never hit the real Toobit API.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import probe_all_symbols as probe


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def tmp_output_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _valid_contract(symbol="BTC-SWAP-USDT"):
    return {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "marginToken": "USDT",
        "contractMultiplier": 0.001,
        "filters": {
            "minQty": 0.001,
            "stepSize": 0.001,
            "tickSize": 0.1,
            "minNotional": 5.0,
        },
        "maxLeverage": 20,
    }


def _valid_specs(symbol="BTC-SWAP-USDT"):
    return {
        "symbol": symbol,
        "quote_asset": "USDT",
        "margin_token": "USDT",
        "contract_multiplier": 0.001,
        "min_qty": 0.001,
        "qty_step": 0.001,
        "tick_size": 0.1,
        "min_notional": 5.0,
        "max_leverage": 20,
    }


# ============================================================
# validate_contract
# ============================================================

def test_validate_contract_accepts_valid(monkeypatch):
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())

    assert specs is not None
    assert specs["symbol"] == "BTC-SWAP-USDT"
    assert errors == []


def test_validate_contract_rejects_non_dict():
    specs, errors, warnings = probe.validate_contract("not a dict")
    assert specs is None
    assert any("not a JSON object" in e for e in errors)


def test_validate_contract_rejects_bad_symbol_format(monkeypatch):
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs("BAD"))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    contract = _valid_contract()
    contract["symbol"] = "BAD"

    specs, errors, warnings = probe.validate_contract(contract)
    assert specs is None
    assert any("Unsupported symbol format" in e for e in errors)


def test_validate_contract_rejects_missing_parse(monkeypatch):
    def _raise(c):
        raise KeyError("contractMultiplier")

    monkeypatch.setattr(probe, "parse_symbol_specs", _raise)

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("Cannot parse" in e for e in errors)


def test_validate_contract_rejects_wrong_quote_asset(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["quote_asset"] = "BTC"

    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("quote_asset must be USDT" in e for e in errors)


def test_validate_contract_rejects_wrong_margin_token(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["margin_token"] = "BTC"

    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("margin_token must be USDT" in e for e in errors)


def test_validate_contract_rejects_negative_min_qty(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["min_qty"] = -0.001

    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("min_qty" in e for e in errors)


def test_validate_contract_rejects_zero_multiplier(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["contract_multiplier"] = 0

    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("contract_multiplier" in e for e in errors)


def test_validate_contract_accepts_missing_max_leverage(monkeypatch):
    specs_ok = _valid_specs()
    specs_ok["max_leverage"] = None

    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: specs_ok)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is not None
    assert any("leverage" in w.lower() for w in warnings)


def test_validate_contract_propagates_validate_specs_errors(monkeypatch):
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(
        probe, "validate_specs",
        lambda s: (["custom validation failed"], []),
    )

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert "custom validation failed" in errors


def test_validate_contract_rejects_malformed_validate_specs(monkeypatch):
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(probe, "validate_specs", lambda s: "not a tuple")

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("unexpected shape" in e for e in errors)


# ============================================================
# atomic_write_json
# ============================================================

def test_atomic_write_creates_file(tmp_output_dir):
    path = tmp_output_dir / "test.json"
    payload = {"schema_version": 1, "test": "ok"}

    probe.atomic_write_json(path, payload)

    assert path.exists()
    with open(path) as f:
        loaded = json.load(f)
    assert loaded == payload


def test_atomic_write_preserves_previous_on_failure(tmp_output_dir, monkeypatch):
    path = tmp_output_dir / "test.json"
    original = {"original": True}
    with open(path, "w") as f:
        json.dump(original, f)

    def failing_replace(src, dst):
        raise OSError("Simulated replace failure")

    monkeypatch.setattr(probe.os, "replace", failing_replace)

    with pytest.raises(OSError):
        probe.atomic_write_json(path, {"new": True})

    with open(path) as f:
        assert json.load(f) == original

    temp_files = [f for f in os.listdir(tmp_output_dir) if f.startswith(".test.json.")]
    assert temp_files == []


def test_atomic_write_creates_parent_dir(tmp_output_dir):
    nested = tmp_output_dir / "a" / "b" / "test.json"
    probe.atomic_write_json(nested, {"ok": True})
    assert nested.exists()


# ============================================================
# discover_symbols
# ============================================================

def test_discover_rejects_non_dict_response(monkeypatch):
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: "not a dict")
    with pytest.raises(RuntimeError, match="not a JSON object"):
        probe.discover_symbols()


def test_discover_rejects_api_error_status(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"status": "API_ERROR", "error": "rate limit"},
    )
    with pytest.raises(RuntimeError, match="API error"):
        probe.discover_symbols()


def test_discover_rejects_empty_contracts(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": []},
    )
    with pytest.raises(RuntimeError, match="empty 'contracts'"):
        probe.discover_symbols()


def test_discover_rejects_missing_contracts(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"other_field": 1},
    )
    with pytest.raises(RuntimeError, match="Missing or empty 'contracts'"):
        probe.discover_symbols()


def test_discover_rejects_when_no_valid_contracts(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": [{"symbol": "INVALID"}]},
    )
    with pytest.raises(RuntimeError, match="No valid USDT-M"):
        probe.discover_symbols()


def test_discover_accepts_valid_contract(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": [_valid_contract()]},
    )
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, warnings = probe.discover_symbols()

    assert payload["count"] == 1
    assert payload["rejected_count"] == 0
    assert payload["symbols"][0]["symbol"] == "BTC-SWAP-USDT"
    assert payload["schema_version"] == 1
    assert "generated_at_utc" in payload


def test_discover_rejects_duplicate_symbols(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": [_valid_contract(), _valid_contract()]},
    )
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, warnings = probe.discover_symbols()

    assert payload["count"] == 1
    assert payload["rejected_count"] == 1
    assert any("Duplicate" in str(r["errors"]) for r in payload["rejected"])


def test_discover_mixes_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {
            "contracts": [
                _valid_contract("BTC-SWAP-USDT"),
                _valid_contract("INVALID"),
                _valid_contract("ETH-SWAP-USDT"),
            ]
        },
    )

    def _parse(c):
        if c["symbol"] == "INVALID":
            raise ValueError("bad")
        return _valid_specs(c["symbol"])

    monkeypatch.setattr(probe, "parse_symbol_specs", _parse)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, warnings = probe.discover_symbols()

    assert payload["count"] == 2
    assert payload["rejected_count"] == 1
    accepted_symbols = {s["symbol"] for s in payload["symbols"]}
    assert accepted_symbols == {"BTC-SWAP-USDT", "ETH-SWAP-USDT"}


def test_discover_sorts_accepted_alphabetically(monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {
            "contracts": [
                _valid_contract("SOL-SWAP-USDT"),
                _valid_contract("BTC-SWAP-USDT"),
                _valid_contract("ETH-SWAP-USDT"),
            ]
        },
    )
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols()
    symbols = [s["symbol"] for s in payload["symbols"]]
    assert symbols == sorted(symbols)


# ============================================================
# main() — end-to-end
# ============================================================

def test_main_success_writes_file(tmp_output_dir, monkeypatch):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": [_valid_contract()]},
    )
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    output_path = tmp_output_dir / "whitelist.json"
    exit_code = probe.main(output_path=output_path)

    assert exit_code == 0
    assert output_path.exists()

    with open(output_path) as f:
        data = json.load(f)
    assert data["count"] == 1
    assert data["schema_version"] == 1


def test_main_failure_preserves_existing_file(tmp_output_dir, monkeypatch):
    output_path = tmp_output_dir / "whitelist.json"
    original = {"schema_version": 1, "count": 42, "symbols": []}
    with open(output_path, "w") as f:
        json.dump(original, f)

    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": []})

    exit_code = probe.main(output_path=output_path)

    assert exit_code == 1

    with open(output_path) as f:
        assert json.load(f) == original


def test_main_preserves_file_on_network_error(tmp_output_dir, monkeypatch):
    output_path = tmp_output_dir / "whitelist.json"
    original = {"schema_version": 1, "count": 1, "symbols": []}
    with open(output_path, "w") as f:
        json.dump(original, f)

    def _raise():
        raise ConnectionError("network down")

    monkeypatch.setattr(probe, "fetch_exchange_info", _raise)

    exit_code = probe.main(output_path=output_path)
    assert exit_code == 1

    with open(output_path) as f:
        assert json.load(f) == original