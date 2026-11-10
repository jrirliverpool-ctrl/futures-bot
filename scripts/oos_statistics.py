"""Predeclared OOS portfolio statistic and circular block bootstrap."""

from datetime import date, timedelta
import math
import random


def _percentile(sorted_values, probability):
    """Linear-interpolated percentile, matching the common type-7 convention."""
    if not sorted_values:
        raise ValueError("cannot calculate a percentile from no values")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def bootstrap_portfolio_profit_factor(
    daily_symbol_pnl,
    *,
    symbols=("BTC", "ETH", "SOL"),
    block_days=7,
    replicates=10_000,
    seed,
):
    """Return the predeclared 95% circular moving-block CI for portfolio PF.

    Input rows are UTC calendar days with one net-PnL value per requested
    symbol. Symbol PnL is aggregated before resampling, preserving same-day
    cross-sectional dependence. Zero-PnL calendar days are required.
    """
    symbols = tuple(symbols)
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("symbols must be a non-empty unique sequence")
    if isinstance(block_days, bool) or not isinstance(block_days, int) or block_days < 1:
        raise ValueError("block_days must be a positive integer")
    if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
        raise ValueError("replicates must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer recorded with the OOS artifact")

    rows = list(daily_symbol_pnl)
    if len(rows) < max(14, block_days * 2):
        raise ValueError("at least two blocks and 14 daily observations are required")

    portfolio = []
    previous = None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"date", *symbols}:
            raise ValueError("each row must contain exactly date and all requested symbols")
        try:
            day = date.fromisoformat(row["date"])
        except (TypeError, ValueError) as exc:
            raise ValueError("dates must be ISO UTC calendar dates") from exc
        if previous is not None and day != previous + timedelta(days=1):
            raise ValueError("daily series must be sorted, unique, and include zero-PnL days")
        previous = day
        values = [row[symbol] for symbol in symbols]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values):
            raise ValueError("daily symbol PnL values must be finite numbers")
        portfolio.append(math.fsum(values))

    def profit_factor(values):
        gains = math.fsum(value for value in values if value > 0)
        losses = -math.fsum(value for value in values if value < 0)
        return gains / losses if losses > 0 else math.inf

    observed_pf = profit_factor(portfolio)
    net_pnl = math.fsum(portfolio)
    rng = random.Random(seed)
    n = len(portfolio)
    samples = []
    non_finite = 0
    for _ in range(replicates):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(portfolio[(start + offset) % n] for offset in range(block_days))
        value = profit_factor(sample[:n])
        if not math.isfinite(value):
            non_finite += 1
        samples.append(value)

    samples.sort()
    invalid_fraction = non_finite / replicates
    inconclusive = invalid_fraction > 0.01 or net_pnl <= 0
    lower = _percentile(samples, 0.025)
    upper = _percentile(samples, 0.975)
    return {
        "statistic": "portfolio_daily_net_profit_factor",
        "observed_profit_factor": observed_pf,
        "net_pnl": net_pnl,
        "confidence_level": 0.95,
        "lower_bound": lower,
        "upper_bound": upper,
        "block_days": block_days,
        "replicates": replicates,
        "seed": seed,
        "non_finite_fraction": invalid_fraction,
        "inconclusive": inconclusive,
        "passes": not inconclusive and lower > 1.0,
    }
