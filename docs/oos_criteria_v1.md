# ApexQuant V1 OOS Protocol

**Version:** v1.1.0-draft
**Status:** NOT APPROVED — OOS execution remains blocked
**Strategy parameters:** frozen during this protocol

This document separates software correctness from evidence of a statistical
edge. Passing tests does not pass OOS, and no Test-period result may be used to
select or retune parameters.

## 1. Intended confirmation run

- Instruments: BTC, ETH, SOL USDT-M futures.
- Data target: about 18 months of quality-checked hourly bars and matching
  historical funding settlements for every instrument.
- Chronological split: 60% Training, 20% Validation, 20% final Test. Do not
  shuffle timestamps.
- Exclude the first three months as an adaptation/warm-up window before
  evaluating the split.
- Minimum final-Test sample: 30 closed trades total, at least 2 traded
  instruments, and at least 5 closed trades for each instrument counted toward
  that minimum.
- Report each instrument and LONG/SHORT separately as well as the portfolio.

## 2. Blockers before the confirmation run

1. **Funding inputs are available locally, but not yet a portable OOS artifact.**
   The strict importer was validated against live read-only Toobit probe reports
   for BTC, ETH, and SOL. Each report contained 2,500 unique records with
   continuous 8-hour settlements; a 2025-05-10 through 2026-11-09 slice contains
   1,647 settlements per symbol. CSVs, raw reports, and SHA-256 manifest are in
   the locally ignored `data/oos/funding/` directory. The data must be reviewed
   for contract-history changes and committed or otherwise pinned for CI before
   a reproducible OOS run. Funding alone does not validate candles or costs.
2. **Toobit candle history does not cover the OOS window through the public
   endpoint.** The new range downloader fails closed on missing/irregular bars.
   A live query for 2025-05-10 returned an empty response, while a recent
   24-hour request returned 24 hourly bars. Do not use the returned recent
   window as the 18-month OOS dataset. Obtain a complete, provenance-backed
   futures candle archive before evaluating the frozen Test split.
3. **Costs are not yet account-calibrated.** Backtest uses configured taker
   fees and a fixed slippage percentage. Neither is a measured fee tier for the
   intended Toobit account; bid/ask spread, market impact, and execution delay
   are not represented as observed historical data.
4. **Protocol statistic is not frozen.** The prior proposal says the bootstrap
   confidence interval's lower bound must exceed 1 for a net profitability
   ratio, but it does not define that ratio, the resampling unit, block length,
   interval level, or treatment of multiple symbols. Freeze these before
   opening final Test results. Do not interpret a trade-level IID bootstrap as
   valid without checking serial dependence.
5. **Dataset integrity is unproven.** There is no signed manifest here for
   source, retrieval time, gaps, duplicate candles, timezone, contract changes,
   symbol listing history, or exact split boundaries.

The current code marks `metrics.funding_modelled` and records funding cashflow.
Ordinary exploratory runs without funding data report Funding as unmodelled;
they are not qualifying net-cost OOS results. The strict OOS path must set
`require_funding=True` and provide a schedule that matches the historical
source.

## 3. Required net-cost model

The evaluated run must account for entry and exit fees, actual funding at every
settlement while a position is open, spread, slippage, contract multiplier,
quantity step/minimum notional, and execution timing. Costs must be deducted
from equity once. Reconcile ledger totals so fees or funding are not counted
both as fill costs and balance flows.

If any required input is unavailable or incomplete, stop and mark the run
**inconclusive**. Do not fill missing funding with zero or silently extrapolate
account fees.

## 4. Metrics and reporting

Report net return, profit factor, expectancy, maximum drawdown, Sharpe and
Sortino (with their sampling frequency stated), win rate, average win/loss,
trade count and holding time. Break down LONG/SHORT, symbol, and predeclared
market regimes. Preserve the exact strategy hash, data manifest, split
boundaries, cost inputs, software revision, raw result, and calculation
version.

Minimum statistical criteria remain provisional until the statistic in
Blocker 3 is frozen. In particular, the proposed lower confidence bound > 1
does not pass until its underlying ratio and bootstrap method are specified.

## 5. Decision rule

Only a fully reproducible Test run that passes every predeclared criterion may
be labelled OOS-passing. If it fails, analyze the failure without tuning on
Test; any revised strategy requires a new untouched Test period. No OOS result
alone authorizes LIVE: the separate `LIVE_READINESS_GATE.md` must also pass.
