from datetime import date, timedelta
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from oos_statistics import bootstrap_portfolio_profit_factor


def _rows(count=140):
    start = date(2025, 1, 1)
    rows = []
    for index in range(count):
        pnl = 2.0 if index % 2 == 0 else -1.0
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "BTC": pnl,
            "ETH": 0.0,
            "SOL": 0.0,
        })
    return rows


def test_profit_factor_bootstrap_is_deterministic_and_includes_zero_days():
    rows = _rows()
    rows[2]["BTC"] = 0.0
    result = bootstrap_portfolio_profit_factor(rows, replicates=1000, seed=71)
    repeat = bootstrap_portfolio_profit_factor(rows, replicates=1000, seed=71)
    assert result == repeat
    assert result["observed_profit_factor"] == pytest.approx(138 / 70)
    assert result["net_pnl"] == 68
    assert result["block_days"] == 7
    assert result["passes"] is True


def test_no_loss_replicates_above_threshold_are_inconclusive():
    rows = _rows(28)
    for row in rows:
        row["BTC"] = 1.0
    result = bootstrap_portfolio_profit_factor(rows, replicates=100, seed=3)
    assert result["observed_profit_factor"] == float("inf")
    assert result["non_finite_fraction"] == 1.0
    assert result["inconclusive"] is True
    assert result["passes"] is False


def test_daily_series_must_include_gaps_as_zero_pnl_days():
    rows = _rows(28)
    del rows[4]
    with pytest.raises(ValueError, match="zero-PnL days"):
        bootstrap_portfolio_profit_factor(rows, replicates=10, seed=1)


def test_each_symbol_is_required_on_every_day():
    rows = _rows(28)
    del rows[0]["ETH"]
    with pytest.raises(ValueError, match="exactly date"):
        bootstrap_portfolio_profit_factor(rows, replicates=10, seed=1)
