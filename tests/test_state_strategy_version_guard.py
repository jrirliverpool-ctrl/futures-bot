
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "scripts",
    ),
)

from state_manager import (
    default_state,
    save_state,
    load_state,
)


def test_strict_load_accepts_matching_strategy(tmp_path):
    state = default_state(1000.0)

    state_path = tmp_path / "state.json"

    save_state(
        str(state_path),
        state,
    )

    loaded = load_state(
        str(state_path),
        strict=True,
    )

    assert loaded["strategy_version"] == state["strategy_version"]
    assert loaded["strategy_hash"] == state["strategy_hash"]


def test_strict_load_rejects_version_mismatch(tmp_path):
    state = default_state(1000.0)

    state["strategy_version"] = "1.0.1"

    state_path = tmp_path / "state.json"

    save_state(
        str(state_path),
        state,
    )

    with pytest.raises(
        ValueError,
        match="Strategy version mismatch",
    ):
        load_state(
            str(state_path),
            strict=True,
        )


def test_non_strict_load_allows_migration_inspection(tmp_path):
    state = default_state(1000.0)

    state["strategy_version"] = "1.0.1"

    state_path = tmp_path / "state.json"

    save_state(
        str(state_path),
        state,
    )

    loaded = load_state(
        str(state_path),
        strict=False,
    )

    assert loaded["strategy_version"] == "1.0.1"


def test_strict_load_rejects_hash_mismatch(tmp_path):
    state = default_state(1000.0)

    state["strategy_hash"] = "sha256:invalid"

    state_path = tmp_path / "state.json"

    save_state(
        str(state_path),
        state,
    )

    with pytest.raises(
        ValueError,
        match="Strategy hash mismatch",
    ):
        load_state(
            str(state_path),
            strict=True,
        )
