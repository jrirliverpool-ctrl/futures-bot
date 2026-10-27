"""tests/test_probe_all_symbols.py — Unit tests for probe_all_symbols.

All network calls are mocked. Tests never hit the real Toobit API.
"""

import json
import math
import os
import sys
import tempfile
from pathlib import Path

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


@pytest.fixture
def crypto_whitelist():
    return {"BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT"}


def _valid_contract(symbol="BTC-SWAP-USDT"):
    """Contract matching real Toobit filters structure.

    filters is a LIST of dicts, each with `filterType`.
    MIN_NOTIONAL uses `minNotional` (not `notional`) — verified against
    the real parser expectation in symbol_specs.py.
    """
    return {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "marginToken": "USDT",
        "contractMultiplier": "0.001",
        "maxLeverage": "20",
        "filters": [
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            },
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": "0.1",
            },
            {
                "filterType": "MIN_NOTIONAL",
                "minNotional": "5.0",
            },
        ],
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
# load_crypto_whitelist
# ============================================================

def test_load_crypto_whitelist_accepts_valid(tmp_output_dir):
    path = tmp_output_dir / "crypto_whitelist.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "symbols": ["BTC-SWAP-USDT", "ETH-SWAP-USDT"],
    }))
    result = probe.load_crypto_whitelist(path)
    assert result == {"BTC-SWAP-USDT", "ETH-SWAP-USDT"}


def test_load_crypto_whitelist_rejects_missing(tmp_output_dir):
    path = tmp_output_dir / "nonexistent.json"
    with pytest.raises(RuntimeError, match="not found"):
        probe.load_crypto_whitelist(path)


def test_load_crypto_whitelist_rejects_empty_list(tmp_output_dir):
    path = tmp_output_dir / "crypto_whitelist.json"
    path.write_text(json.dumps({"schema_version": 1, "symbols": []}))
    with pytest.raises(RuntimeError, match="non-empty list"):
        probe.load_crypto_whitelist(path)


def test_load_crypto_whitelist_rejects_bad_format(tmp_output_dir):
    path = tmp_output_dir / "crypto_whitelist.json"
    path.write_text(json.dumps({"schema_version": 1, "symbols": ["BAD"]}))
    with pytest.raises(RuntimeError, match="Invalid symbol"):
        probe.load_crypto_whitelist(path)


def test_load_crypto_whitelist_rejects_duplicate(tmp_output_dir):
    path = tmp_output_dir / "crypto_whitelist.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "symbols": ["BTC-SWAP-USDT", "BTC-SWAP-USDT"],
    }))
    with pytest.raises(RuntimeError, match="Duplicate"):
        probe.load_crypto_whitelist(path)


# ============================================================
# Real-parser smoke test (no mock)
# ============================================================

def test_parse_symbol_specs_real_function_accepts_fixture():
    """Verify our fixture matches what the REAL parser expects.

    This test does NOT mock the parser — it verifies that our _valid_contract()
    has the right structure (filters list, minNotional key, string numbers).
    If it fails, our fixture (or the parser) has drifted.
    """
    from symbol_specs import parse_symbol_specs

    contract = _valid_contract("BTC-SWAP-USDT")
    specs = parse_symbol_specs(contract)

    assert isinstance(specs, dict)
    assert specs["symbol"] == "BTC-SWAP-USDT"
    assert isinstance(specs["contract_multiplier"], (int, float))
    assert specs["contract_multiplier"] > 0
    assert isinstance(specs["min_qty"], (int, float))
    assert specs["min_qty"] > 0
    assert isinstance(specs["qty_step"], (int, float))
    assert specs["qty_step"] > 0
    assert isinstance(specs["tick_size"], (int, float))
    assert specs["tick_size"] > 0
    assert isinstance(specs["min_notional"], (int, float))
    assert specs["min_notional"] >= 0


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
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
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


def test_validate_contract_rejects_nan_multiplier(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["contract_multiplier"] = float("nan")
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("contract_multiplier" in e for e in errors)


def test_validate_contract_rejects_inf_tick_size(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["tick_size"] = float("inf")
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("tick_size" in e for e in errors)


def test_validate_contract_rejects_negative_min_notional(monkeypatch):
    bad_specs = _valid_specs()
    bad_specs["min_notional"] = -1.0
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: bad_specs)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    specs, errors, warnings = probe.validate_contract(_valid_contract())
    assert specs is None
    assert any("min_notional" in e for e in errors)


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
# discover_symbols — filtering + status
# ============================================================

def test_discover_complete_when_all_symbols_found(monkeypatch, crypto_whitelist):
    contracts = [
        _valid_contract("BTC-SWAP-USDT"),
        _valid_contract("ETH-SWAP-USDT"),
        _valid_contract("SOL-SWAP-USDT"),
        _valid_contract("AAPL-SWAP-USDT"),
    ]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols(crypto_whitelist)

    assert payload["status"] == "complete"
    assert payload["count"] == 3
    assert payload["missing_from_api_count"] == 0
    assert payload["rejected_count"] == 0
    assert payload["excluded_not_allowlisted_count"] == 1


def test_discover_incomplete_when_symbol_missing(monkeypatch, crypto_whitelist):
    contracts = [
        _valid_contract("BTC-SWAP-USDT"),
        _valid_contract("ETH-SWAP-USDT"),
        # SOL is missing
    ]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols(crypto_whitelist)

    assert payload["status"] == "incomplete"
    assert payload["count"] == 2
    assert payload["missing_from_api_count"] == 1
    assert "SOL-SWAP-USDT" in payload["missing_from_api"]


def test_discover_incomplete_when_symbol_rejected(monkeypatch, crypto_whitelist):
    def _parse(c):
        if c["symbol"] == "SOL-SWAP-USDT":
            raise ValueError("bad multiplier")
        return _valid_specs(c["symbol"])

    contracts = [
        _valid_contract("BTC-SWAP-USDT"),
        _valid_contract("ETH-SWAP-USDT"),
        _valid_contract("SOL-SWAP-USDT"),
    ]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    monkeypatch.setattr(probe, "parse_symbol_specs", _parse)
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols(crypto_whitelist)

    assert payload["status"] == "incomplete"
    assert payload["count"] == 2
    assert payload["rejected_count"] == 1


def test_discover_reports_missing_symbols(monkeypatch, crypto_whitelist):
    contracts = [_valid_contract("BTC-SWAP-USDT")]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols(crypto_whitelist)
    assert payload["missing_from_api_count"] == 2
    assert sorted(payload["missing_from_api"]) == ["ETH-SWAP-USDT", "SOL-SWAP-USDT"]


def test_discover_raises_if_no_crypto_found(monkeypatch, crypto_whitelist):
    contracts = [_valid_contract("AAPL-SWAP-USDT")]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    with pytest.raises(RuntimeError, match="No valid USDT-M swap"):
        probe.discover_symbols(crypto_whitelist)


def test_discover_rejects_non_dict_response(monkeypatch, crypto_whitelist):
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: "not a dict")
    with pytest.raises(RuntimeError, match="not a JSON object"):
        probe.discover_symbols(crypto_whitelist)


def test_discover_rejects_api_error_status(monkeypatch, crypto_whitelist):
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"status": "API_ERROR", "error": "rate limit"},
    )
    with pytest.raises(RuntimeError, match="API error"):
        probe.discover_symbols(crypto_whitelist)


def test_discover_rejects_empty_contracts(monkeypatch, crypto_whitelist):
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": []})
    with pytest.raises(RuntimeError, match="empty 'contracts'"):
        probe.discover_symbols(crypto_whitelist)


def test_discover_rejects_missing_contracts(monkeypatch, crypto_whitelist):
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"other_field": 1})
    with pytest.raises(RuntimeError, match="Missing or empty 'contracts'"):
        probe.discover_symbols(crypto_whitelist)


def test_discover_rejects_duplicates(monkeypatch, crypto_whitelist):
    contracts = [_valid_contract("BTC-SWAP-USDT"), _valid_contract("BTC-SWAP-USDT")]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs())
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols(crypto_whitelist)
    assert payload["count"] == 1
    assert payload["rejected_count"] == 1


def test_discover_sorts_accepted_alphabetically(monkeypatch, crypto_whitelist):
    contracts = [
        _valid_contract("SOL-SWAP-USDT"),
        _valid_contract("BTC-SWAP-USDT"),
        _valid_contract("ETH-SWAP-USDT"),
    ]
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": contracts})
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    payload, _ = probe.discover_symbols(crypto_whitelist)
    symbols = [s["symbol"] for s in payload["symbols"]]
    assert symbols == sorted(symbols)


# ============================================================
# main — exit codes
# ============================================================

def test_main_returns_0_on_complete(tmp_output_dir, monkeypatch, crypto_whitelist):
    monkeypatch.setattr(probe, "load_crypto_whitelist", lambda p: crypto_whitelist)
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": [
            _valid_contract("BTC-SWAP-USDT"),
            _valid_contract("ETH-SWAP-USDT"),
            _valid_contract("SOL-SWAP-USDT"),
        ]},
    )
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    out = tmp_output_dir / "whitelist.json"
    rc = probe.main(output_path=out)

    assert rc == probe.EXIT_COMPLETE
    assert out.exists()
    with open(out) as f:
        data = json.load(f)
    assert data["status"] == "complete"


def test_main_returns_2_on_incomplete(tmp_output_dir, monkeypatch, crypto_whitelist):
    monkeypatch.setattr(probe, "load_crypto_whitelist", lambda p: crypto_whitelist)
    monkeypatch.setattr(
        probe, "fetch_exchange_info",
        lambda: {"contracts": [
            _valid_contract("BTC-SWAP-USDT"),
            _valid_contract("ETH-SWAP-USDT"),
        ]},  # SOL missing
    )
    monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
    monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

    out = tmp_output_dir / "whitelist.json"
    rc = probe.main(output_path=out)

    assert rc == probe.EXIT_INCOMPLETE
    assert out.exists()  # report is still written for diagnosis
    with open(out) as f:
        data = json.load(f)
    assert data["status"] == "incomplete"
    assert data["missing_from_api"] == ["SOL-SWAP-USDT"]


def test_main_refuses_to_overwrite_canonical_with_incomplete(monkeypatch, crypto_whitelist):
    """If output is canonical path and result is incomplete, don't overwrite."""
    canonical = probe.DEFAULT_OUTPUT_PATH
    original = {"schema_version": 1, "status": "complete", "count": 10, "symbols": []}

    # Write a "good" canonical state
    canonical.parent.mkdir(parents=True, exist_ok=True)
    with open(canonical, "w") as f:
        json.dump(original, f)

    try:
        monkeypatch.setattr(probe, "load_crypto_whitelist", lambda p: crypto_whitelist)
        monkeypatch.setattr(
            probe, "fetch_exchange_info",
            lambda: {"contracts": [_valid_contract("BTC-SWAP-USDT")]},  # incomplete
        )
        monkeypatch.setattr(probe, "parse_symbol_specs", lambda c: _valid_specs(c["symbol"]))
        monkeypatch.setattr(probe, "validate_specs", lambda s: ([], []))

        rc = probe.main(output_path=canonical)

        assert rc == probe.EXIT_INCOMPLETE
        # Canonical file must be UNTOUCHED
        with open(canonical) as f:
            data = json.load(f)
        assert data["count"] == 10
        assert data["status"] == "complete"
    finally:
        if canonical.exists():
            canonical.unlink()


def test_main_returns_1_on_failure(tmp_output_dir, monkeypatch, crypto_whitelist):
    monkeypatch.setattr(probe, "load_crypto_whitelist", lambda p: crypto_whitelist)
    monkeypatch.setattr(probe, "fetch_exchange_info", lambda: {"contracts": []})

    out = tmp_output_dir / "whitelist.json"
    rc = probe.main(output_path=out)

    assert rc == probe.EXIT_FAILED
    assert not out.exists()


def test_main_fails_if_crypto_whitelist_missing(tmp_output_dir, monkeypatch):
    def _raise(_p):
        raise RuntimeError("crypto_whitelist.json not found")

    monkeypatch.setattr(probe, "load_crypto_whitelist", _raise)

    out = tmp_output_dir / "whitelist.json"
    rc = probe.main(output_path=out)

    assert rc == probe.EXIT_FAILED
    assert not out.exists()