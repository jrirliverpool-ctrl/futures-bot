import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "toobit_private_test.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("toobit_private_test_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_without_keys_is_safe_and_does_not_make_requests(monkeypatch):
    monkeypatch.delenv("TOOBIT_API_KEY", raising=False)
    monkeypatch.delenv("TOOBIT_API_SECRET", raising=False)
    module = _load_script()
    with pytest.raises(RuntimeError, match="not configured"):
        module.private_get("/api/v1/futures/balance")


def test_main_without_keys_stops_before_network(monkeypatch):
    monkeypatch.delenv("TOOBIT_API_KEY", raising=False)
    monkeypatch.delenv("TOOBIT_API_SECRET", raising=False)
    module = _load_script()
    with pytest.raises(SystemExit, match="not configured"):
        module.main()
