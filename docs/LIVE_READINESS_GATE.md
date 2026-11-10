# ApexQuant LIVE Readiness Gate

**Status:** BLOCKED — no live order authorization
**Assessment base:** `main` at `ac8e18e`; candidate branch `codex/apexquant-hedge-funding-readiness`
**Last assessed:** 2026-11-10

This gate distinguishes implementation evidence from strategy evidence. A
passing software test does not demonstrate profitability, and a successful
read-only account check does not authorize order placement. Every item requires
current, reproducible evidence before the first live order.

| Gate | Current status | Evidence required to pass |
| --- | --- | --- |
| 1. Hedge-mode State and Reconciliation | Local state, reconciliation, and order-preflight helpers are side-aware; repeated core position snapshots fail closed on changes; unit tests cover both sides | CI passed for commit `c508100`; dual-side live-account snapshots and recovery remain unverified |
| 2. Order and Fill Recovery | V2 order-side mapping and single-attempt UNKNOWN handling are unit-tested; normal and conditional open orders have stable-pagination helpers | CI passed for commit `c508100`; unresolved-order recovery with a durable intent journal is not implemented end-to-end |
| 3. Fill, Fee, Funding, and Equity Ledger | Partial implementation; net fill commissions now report commission minus maker rebate by asset | Reconcile fills, fee and funding flows, realized PnL, and equity against a real account export with duplicate-flow protection |
| 4. Durable State and Restart Recovery | Local file state is atomic; no durable live backend verified | Demonstrate restart and partial-fill recovery on persistent storage; prove no duplicate submission after UNKNOWN outcomes |
| 5. Portfolio Risk, Margin, and Circuit Breakers | Local V2 paper engine now enforces aggregate open, pending, and proposed stop-risk against `max_portfolio_risk`; unknown legacy position risk fails closed. Sizing includes round-trip fees and adverse stop slippage. Exchange margin and stress exposure remain unverified | Validate portfolio-wide and stress exposure, forward funding, exchange margin/liquidation distance, and all breaker triggers against current account/contract inputs |
| 6. Toobit Execution API and Protection | Not approved; official V2 field mapping is implemented and locally tested; account-side semantics have not been exercised | Verify exact open/close, protective-order, precision, and error semantics for the account's position mode; place and verify protection only in a controlled non-live environment first, then verify installed SL/TP with read-only account evidence |
| 7. OOS Strategy Validation | NOT APPROVED; 18-month funding is local, Toobit candle history unavailable for the window | The Toobit kline endpoint returned no data for the required 2025-05-10 start; acquire a provenance-backed complete futures candle archive, pin data, calibrate costs, implement and verify the now-specified bootstrap statistic, then run untouched chronological Test data |
| 8. Continuous Paper Trading | Not evidenced for the current candidate | At least 14 continuous days, preferably 30, with real market data, persistent state, costs, and no critical reconciliation or accounting gaps |
| 9. Mock Live and Fault Recovery | Prior results apply to earlier revisions only | Run the complete required failure matrix on this exact candidate and preserve logs/artifacts |
| 10. Real-account Read-only Preflight and Security | Not run | Repeated read-only balance, position, open-order, fee, and funding reconciliation; verify API permissions, secret handling, and independent kill switch |

## Current local verification

- Checkout base: `ac8e18e` (`main`); the earlier short identifier `e1002b6` is not present in this repository history.
- Candidate branch is `codex/apexquant-hedge-funding-readiness`, tracking GitHub. Commit `c508100` passed all 17 GitHub Actions jobs, including the integrated account-audit and conditional-order pagination tests.
- Previous full suite: **1221 passed** on Python 3.12 / Windows. The new integration checks prove an incomplete conditional-order snapshot prevents ledger mutation, and a stable protective-order snapshot appears in the report. Run the full suite again before treating this exact working tree as verified.
- No live Toobit credentials or account read have been used in this task. No exchange order has been sent.
- Account audit scans both `STOP` and `STOP_PROFIT_LOSS`; Toobit's default conditional-order query omits TP/SL. CI for this scan passed on `c508100`; the new integration-failure tests are pending a CI run.

## Explicit stop conditions

Keep live trading disabled if any of these are true:

- A position, order, or account-flow snapshot is incomplete, stale, duplicated,
  or inconsistent with local state.
- An order outcome is UNKNOWN, a fill is partial or unaccounted for, or a
  protective order cannot be verified at the exchange.
- Any fee, funding, equity, margin, or portfolio-risk discrepancy remains.
- OOS criteria are not finalized or the net-of-costs OOS result fails.
- Any required gate above lacks current evidence.

This file is a status register, not permission to trade. No real order may be
sent based on a green local test suite or on this checklist alone.
