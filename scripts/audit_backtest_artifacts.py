"""
audit_backtest_artifacts.py — Audit ساختار خروجی‌های Smoke Backtest.

این اسکریپت سه خروجی زیر را بازرسی می‌کند:

  reports/equity_curve.csv
  reports/trades.csv
  backtest_state.json

Audit ≠ ارزیابی عملکرد
══════════════════════
این اسکریپت فقط ساختار، نوع‌داده، و invariants حسابداری را
چک می‌کند. اگر استراتژی در پنجره‌ی بک‌تست ضرر کرده باشد،
Audit Fail نمی‌شود.

فرض‌های حسابداری
════════════════
state["equity"] فقط realized PnL را شامل می‌شود
(در paper_engine.py فقط در close_position تغییر می‌کند).

equity_curve.csv شامل MTM است (realized + unrealized).

Cross-file invariants
═════════════════════
  1. Σ(trades.pnl_usd) == state.equity - state.initial_equity
     (همیشه برقرار است، بدون توجه به position)

  2. last equity_mtm == state.equity
     (فقط وقتی position بسته است)

     اگر position باز است:
       equity_mtm - state.equity
         = pos.realized_pnl (از TP1)
         + remaining_size * (last_close - entry) * mult * dir_sign
     این مقدار دقیقاً از state محاسبه و مقایسه می‌شود.
     فیلدهای لازم: symbol, contract_multiplier, last_close_price
     (که run_real_backtest.py اضافه می‌کند)

نحوه استفاده:
  python scripts/audit_backtest_artifacts.py
  python scripts/audit_backtest_artifacts.py <equity.csv> <trades.csv> <state.json>
"""

import csv
import json
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from performance import EQUITY_CSV_COLUMNS, TRADES_CSV_COLUMNS
from state_manager import load_state
from compute_hash import compute_strategy_hash


# ============================================================
# Helpers
# ============================================================

FLOAT_TOL = 1e-6


def _exists(path: str) -> bool:
    if not os.path.exists(path):
        print(f"❌ File not found: {path}")
        return False
    if os.path.getsize(path) == 0:
        print(f"❌ File is empty: {path}")
        return False
    return True


def _read_csv(path: str):
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return reader.fieldnames, rows


# ============================================================
# Equity CSV Audit
# ============================================================

def audit_equity_csv(path: str) -> list:
    errors = []
    print("─" * 66)
    print(f"🔍 Auditing equity CSV: {path}")
    print("─" * 66)

    if not _exists(path):
        errors.append(f"equity CSV missing: {path}")
        return errors

    try:
        header, rows = _read_csv(path)
    except Exception as e:
        errors.append(f"equity CSV read error: {e}")
        return errors

    if tuple(header) != EQUITY_CSV_COLUMNS:
        errors.append(
            f"equity header mismatch\n"
            f"    expected: {EQUITY_CSV_COLUMNS}\n"
            f"    got     : {tuple(header)}"
        )
        return errors
    print(f"  ✅ header matches EQUITY_CSV_COLUMNS")

    if len(rows) == 0:
        errors.append("equity CSV has no rows")
        return errors
    print(f"  ✅ row count: {len(rows)}")

    try:
        indexes = [int(r["candle_index"]) for r in rows]
        if indexes != list(range(len(rows))):
            errors.append(
                f"candle_index not sequential 0..{len(rows)-1}"
            )
        else:
            print(f"  ✅ candle_index sequential 0..{len(rows)-1}")
    except (ValueError, KeyError) as e:
        errors.append(f"candle_index parse error: {e}")

    try:
        ts = [int(r["candle_ts_ms"]) for r in rows]
        if ts != sorted(ts):
            errors.append("candle_ts_ms not monotonically increasing")
        else:
            print(f"  ✅ candle_ts_ms monotonic")
    except (ValueError, KeyError) as e:
        errors.append(f"candle_ts_ms parse error: {e}")

    equity_vals = []
    realized_vals = []
    dd_vals = []
    peak_vals = []
    for i, r in enumerate(rows):
        try:
            equity_vals.append(float(r["equity_mtm"]))
            realized_vals.append(float(r["equity_realized"]))
            dd_vals.append(float(r["drawdown_pct"]))
            peak_vals.append(float(r["peak_equity"]))
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: numeric parse error: {e}")
            break
    if not errors:
        print(f"  ✅ all numeric fields parse")

    if dd_vals and any(d < -FLOAT_TOL for d in dd_vals):
        errors.append("drawdown_pct has negative values")
    else:
        print(f"  ✅ drawdown_pct >= 0")

    if equity_vals and peak_vals:
        violations = [
            (i, e, p) for i, (e, p) in enumerate(zip(equity_vals, peak_vals))
            if p < e - FLOAT_TOL
        ]
        if violations:
            errors.append(
                f"peak_equity < equity_mtm at {len(violations)} rows. "
                f"first: idx={violations[0][0]} "
                f"equity={violations[0][1]} peak={violations[0][2]}"
            )
        else:
            print(f"  ✅ peak_equity >= equity_mtm at all rows")

    if peak_vals:
        monotonic = all(
            peak_vals[i] >= peak_vals[i-1] - FLOAT_TOL
            for i in range(1, len(peak_vals))
        )
        if not monotonic:
            errors.append("peak_equity not monotonic non-decreasing")
        else:
            print(f"  ✅ peak_equity monotonic non-decreasing")

    if rows:
        print(f"\n  📌 first row: {rows[0]}")
        print(f"  📌 last row : {rows[-1]}")

    return errors


# ============================================================
# Trades CSV Audit
# ============================================================

def audit_trades_csv(path: str) -> list:
    errors = []
    print()
    print("─" * 66)
    print(f"🔍 Auditing trades CSV: {path}")
    print("─" * 66)

    if not _exists(path):
        errors.append(f"trades CSV missing: {path}")
        return errors

    try:
        header, rows = _read_csv(path)
    except Exception as e:
        errors.append(f"trades CSV read error: {e}")
        return errors

    if tuple(header) != TRADES_CSV_COLUMNS:
        errors.append(
            f"trades header mismatch\n"
            f"    expected: {TRADES_CSV_COLUMNS}\n"
            f"    got     : {tuple(header)}"
        )
        return errors
    print(f"  ✅ header matches TRADES_CSV_COLUMNS")

    print(f"  ✅ row count: {len(rows)}")

    if len(rows) == 0:
        print("  ℹ️  no trades in this window (valid outcome)")
        return errors

    valid_reasons = {
        "SL", "TP1", "TRAILING_SL", "TIME_EXIT",
        "EXIT_SIGNAL", "TP2",
    }

    for i, r in enumerate(rows):
        if r["side"] not in ("LONG", "SHORT"):
            errors.append(f"row {i}: invalid side: {r['side']}")
            continue

        try:
            e_ts = int(r["entry_ts_ms"])
            x_ts = int(r["exit_ts_ms"])
            if x_ts < e_ts:
                errors.append(
                    f"row {i}: exit_ts ({x_ts}) < entry_ts ({e_ts})"
                )
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: timestamp parse error: {e}")
            continue

        try:
            ep = float(r["entry_price"])
            xp = float(r["exit_price"])
            if ep <= 0 or xp <= 0:
                errors.append(
                    f"row {i}: non-positive price "
                    f"(entry={ep}, exit={xp})"
                )
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: price parse error: {e}")
            continue

        try:
            sz = float(r["size"])
            if sz <= 0:
                errors.append(f"row {i}: size <= 0: {sz}")
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: size parse error: {e}")
            continue

        try:
            float(r["pnl_usd"])
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: pnl parse error: {e}")
            continue

        for field in ("r_multiple", "risk_usd"):
            v = r.get(field, "")
            if v not in ("", None):
                try:
                    float(v)
                except (ValueError, TypeError):
                    errors.append(
                        f"row {i}: {field} non-numeric: {v!r}"
                    )

        if r["exit_reason"] not in valid_reasons:
            errors.append(
                f"row {i}: unknown exit_reason: {r['exit_reason']}"
            )

    if not errors:
        print(f"  ✅ all {len(rows)} rows structurally valid")

    print(f"\n  📌 trade summary:")
    for i, r in enumerate(rows[:20]):
        rm = r.get("r_multiple", "") or "—"
        print(
            f"    [{i}] {r['side']:5s} "
            f"pnl={float(r['pnl_usd']):+.4f} "
            f"R={rm} "
            f"reason={r['exit_reason']}"
        )
    if len(rows) > 20:
        print(f"    ... and {len(rows) - 20} more")

    return errors


# ============================================================
# State JSON Audit
# ============================================================

def audit_state_json(path: str) -> list:
    errors = []
    print()
    print("─" * 66)
    print(f"🔍 Auditing state JSON: {path}")
    print("─" * 66)

    if not _exists(path):
        errors.append(f"state JSON missing: {path}")
        return errors

    try:
        state = load_state(path, strict=False)
    except Exception as e:
        errors.append(f"state load error: {e}")
        return errors
    print(f"  ✅ state loaded and schema-valid")

    current_hash = compute_strategy_hash()
    if state["strategy_hash"] != current_hash:
        errors.append(
            f"state hash mismatch\n"
            f"    state  : {state['strategy_hash']}\n"
            f"    current: {current_hash}"
        )
    else:
        print(f"  ✅ strategy_hash matches current")

    eq = state["equity"]
    peak = state["peak_equity"]
    initial = state["initial_equity"]

    if eq < 0:
        errors.append(f"equity negative: {eq}")
    if peak < eq - FLOAT_TOL:
        errors.append(f"peak_equity ({peak}) < equity ({eq})")
    else:
        print(f"  ✅ peak_equity >= equity")

    if initial <= 0:
        errors.append(f"initial_equity non-positive: {initial}")

    pos = state["position"]
    if pos is not None:
        required_pos_fields = {
            "side", "entry_price", "size", "remaining_size",
            "initial_sl", "current_sl", "initial_tp1",
            "tp1_hit", "be_moved", "trailing_active",
            "candles_since_entry", "atr_at_entry",
        }
        missing = required_pos_fields - set(pos.keys())
        if missing:
            errors.append(
                f"position missing fields: {sorted(missing)}"
            )
        else:
            print(f"  ✅ open position schema valid")
    else:
        print(f"  ✅ no open position")

    print(f"\n  📌 state snapshot:")
    print(f"    version         : {state['version']}")
    print(f"    strategy_version: {state['strategy_version']}")
    print(f"    initial_equity  : {state['initial_equity']}")
    print(f"    equity          : {state['equity']}")
    print(f"    peak_equity     : {state['peak_equity']}")
    print(f"    daily_loss      : {state['daily_loss']}")
    print(f"    weekly_loss     : {state['weekly_loss']}")
    print(f"    CB active       : {state['circuit_breaker_active']}")
    print(f"    has_position    : {state['position'] is not None}")
    print(f"    has_pending     : {state['pending_entry'] is not None}")

    return errors


# ============================================================
# Cross-File Consistency
# ============================================================

def audit_cross_consistency(equity_path, trades_path, state_path) -> list:
    """
    Cross-file invariants.

    Law 1: Σ(trades.pnl_usd) == state.equity - state.initial_equity
        همیشه برقرار است (state.equity فقط realized).

    Law 2: last equity_mtm == state.equity
        اگر position بسته است.

        اگر position باز است:
          last equity_mtm - state.equity
            = pos.realized_pnl + unrealized_remaining
        که unrealized_remaining =
          remaining_size × (last_close − entry) × mult × dir_sign

        محاسبه‌ی دقیق نیاز دارد:
          - state.contract_multiplier
          - state.last_close_price
          (که run_real_backtest.py در state اضافه می‌کند)
    """
    errors = []
    print()
    print("─" * 66)
    print("🔍 Cross-file consistency")
    print("─" * 66)

    with open(trades_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        trade_rows = list(reader)

    state = load_state(state_path, strict=False)

    # ── Law 1: PnL accounting ──
    sum_pnl = sum(float(r["pnl_usd"]) for r in trade_rows)
    delta = state["equity"] - state["initial_equity"]
    if abs(sum_pnl - delta) > 1e-3:
        errors.append(
            f"PnL mismatch\n"
            f"    Σ(trades.pnl_usd) = {sum_pnl:.6f}\n"
            f"    state delta       = {delta:.6f}\n"
            f"    diff              = {sum_pnl - delta:.6f}"
        )
    else:
        print(
            f"  ✅ Σ(trades.pnl_usd) = state delta "
            f"({sum_pnl:.6f})"
        )

    # ── Law 2: MTM vs realized ──
    with open(equity_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        eq_rows = list(reader)

    if not eq_rows:
        return errors

    last_mtm = float(eq_rows[-1]["equity_mtm"])
    state_equity = state["equity"]
    position_open = state["position"] is not None

    if not position_open:
        # strict equality
        if abs(last_mtm - state_equity) > 1e-3:
            errors.append(
                f"last equity_mtm ({last_mtm:.6f}) != "
                f"state equity ({state_equity:.6f}) "
                f"(no open position)"
            )
        else:
            print(
                f"  ✅ last equity_mtm == state equity "
                f"({last_mtm:.6f})"
            )
        return errors

    # ── position is open: exact computation ──
    pos = state["position"]

    required_audit_fields = (
        "contract_multiplier",
        "last_close_price",
    )
    missing = [f for f in required_audit_fields if f not in state]
    if missing:
        errors.append(
            f"state missing audit metadata fields: {missing}\n"
            f"    run_real_backtest.py باید این فیلدها را ذخیره کند."
        )
        return errors

    mult = float(state["contract_multiplier"])
    last_close = float(state["last_close_price"])
    entry = float(pos["entry_price"])
    remaining = float(pos["remaining_size"])
    side = pos["side"]
    tp1_realized = float(pos.get("realized_pnl", 0.0))
    dir_sign = 1 if side == "LONG" else -1

    expected_unrealized = (
        remaining * (last_close - entry) * mult * dir_sign
    )
    expected_mtm_delta = tp1_realized + expected_unrealized
    actual_mtm_delta = last_mtm - state_equity

    print(
        f"  ℹ️  open {side} position detected\n"
        f"      entry           : {entry}\n"
        f"      last_close      : {last_close}\n"
        f"      remaining_size  : {remaining}\n"
        f"      mult            : {mult}\n"
        f"      tp1_realized    : {tp1_realized:+.6f}\n"
        f"      unrealized_exp  : {expected_unrealized:+.6f}\n"
        f"      expected Δ      : {expected_mtm_delta:+.6f}\n"
        f"      actual Δ        : {actual_mtm_delta:+.6f}"
    )

    if abs(expected_mtm_delta - actual_mtm_delta) > 1e-6:
        errors.append(
            f"unrealized PnL mismatch\n"
            f"    expected Δ = {expected_mtm_delta:.9f}\n"
            f"    actual   Δ = {actual_mtm_delta:.9f}\n"
            f"    diff       = {expected_mtm_delta - actual_mtm_delta:.9f}"
        )
    else:
        print(
            f"  ✅ unrealized PnL matches exactly "
            f"({actual_mtm_delta:+.6f})"
        )

    return errors


# ============================================================
# Main
# ============================================================

def main(argv):
    equity_path = argv[1] if len(argv) > 1 else "reports/equity_curve.csv"
    trades_path = argv[2] if len(argv) > 2 else "reports/trades.csv"
    state_path  = argv[3] if len(argv) > 3 else "backtest_state.json"

    print("=" * 66)
    print("🔎 BACKTEST ARTIFACT AUDIT")
    print("=" * 66)
    print(f"  equity: {equity_path}")
    print(f"  trades: {trades_path}")
    print(f"  state : {state_path}")
    print("=" * 66)
    print()

    errors = []
    errors += audit_equity_csv(equity_path)
    errors += audit_trades_csv(trades_path)
    errors += audit_state_json(state_path)

    if (os.path.exists(equity_path) and
        os.path.exists(trades_path) and
        os.path.exists(state_path)):
        try:
            errors += audit_cross_consistency(
                equity_path, trades_path, state_path,
            )
        except Exception as e:
            errors.append(f"cross-consistency audit error: {e}")

    print()
    print("=" * 66)
    if errors:
        print(f"❌ AUDIT FAILED — {len(errors)} issue(s):")
        for i, e in enumerate(errors, 1):
            print(f"  [{i}] {e}")
        print("=" * 66)
        return 1
    print("✅ AUDIT PASSED — all artifacts structurally valid")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))