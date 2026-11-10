# ApexQuant LIVE Readiness Gate

**Status:** BLOCKED — no live order authorization
**Assessment base:** `main` at `ac8e18e` plus the local candidate branch `codex/apexquant-hedge-funding-readiness`
**Last assessed:** 2026-11-10

This gate distinguishes implementation evidence from strategy evidence. A
passing software test does not demonstrate profitability, and a successful
read-only account check does not authorize order placement. Every item requires
current, reproducible evidence before the first live order.

| Gate | Current status | Evidence required to pass |
| --- | --- | --- |
| 1. Hedge-mode State and Reconciliation | Local state, reconciliation, and order-preflight helpers are side-aware; repeated core position snapshots fail closed on changes; focused tests pass | Linux CI on the committed change; dual-side account snapshots and recovery validated against the documented Toobit response schema |
| 2. Order and Fill Recovery | Local V2 read-only normalization fix; covered by tests | Linux CI; complete order/trade pagination and unresolved-order scenarios proven without journal mutation |
| 3. Fill, Fee, Funding, and Equity Ledger | Partial implementation; net fill commissions now report commission minus maker rebate by asset | Reconcile fills, fee and funding flows, realized PnL, and equity against a real account export with duplicate-flow protection |
| 4. Durable State and Restart Recovery | Local file state is atomic; no durable live backend verified | Demonstrate restart and partial-fill recovery on persistent storage; prove no duplicate submission after UNKNOWN outcomes |
| 5. Portfolio Risk, Margin, and Circuit Breakers | Local V2 paper engine now enforces aggregate open, pending, and proposed stop-risk against `max_portfolio_risk`; unknown legacy position risk fails closed. Sizing includes round-trip fees and adverse stop slippage. Exchange margin and stress exposure remain unverified | Validate portfolio-wide and stress exposure, forward funding, exchange margin/liquidation distance, and all breaker triggers against current account/contract inputs |
| 6. Toobit Execution API and Protection | Not approved | Verify the exact order, close, conditional-protection, precision, and error semantics for the account's position mode; verify live SL/TP at exchange |
| 7. OOS Strategy Validation | NOT APPROVED; 18-month funding is local, Toobit candle history unavailable for the window | The Toobit kline endpoint returned no data for the required 2025-05-10 start; acquire a provenance-backed complete futures candle archive, pin data, calibrate costs, implement and verify the now-specified bootstrap statistic, then run untouched chronological Test data |
| 8. Continuous Paper Trading | Not evidenced for the current candidate | At least 14 continuous days, preferably 30, with real market data, persistent state, costs, and no critical reconciliation or accounting gaps |
| 9. Mock Live and Fault Recovery | Prior results apply to earlier revisions only | Run the complete required failure matrix on this exact candidate and preserve logs/artifacts |
| 10. Real-account Read-only Preflight and Security | Not run | Repeated read-only balance, position, open-order, fee, and funding reconciliation; verify API permissions, secret handling, and independent kill switch |

## Current local verification

- Checkout base: `ac8e18e` (`main`); the earlier short identifier `e1002b6` was not found in this repository history.
- Local full test suite: **1204 passed** on Python 3.12 / Windows with standard root discovery (`pytest -q`). The read-only account utility is import-safe and never prints raw account payloads; position reconciliation also requires two matching GET snapshots for the position-defining fields.
- Focused hedge/recovery/OOS-cost suite: **169 passed**, including importer validation.
- `git diff --check`: clean at last run.
- Candidate branch is local and unpushed; GitHub write access denied branch creation (403), so no remote CI result exists.

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
