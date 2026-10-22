"""
audit_equity.py — Strict Independent Equity Audit.
Fails with exit code 1 if state/events are missing or corrupt.
"""
import json
import os
import sys

def main():
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    state_path = os.path.join(state_dir, "state.json")
    events_path = os.path.join(state_dir, "events.jsonl")

    # ✅ 1. بررسی وجود فایل‌ها (Fail-Fast)
    if not os.path.exists(state_path):
        print("❌ ERROR: State file not found.")
        sys.exit(1)
        
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
    except json.JSONDecodeError:
        print("❌ ERROR: State file is corrupted (Invalid JSON).")
        sys.exit(1)

    print("=" * 50)
    print("EQUITY AUDIT")
    print("=" * 50)
    print(f"Current Equity in State: {state['equity']}")
    print(f"Initial Equity: {state['initial_equity']}")

    if not os.path.exists(events_path):
        print("❌ ERROR: Events file not found.")
        sys.exit(1)

    total_gross_pnl = 0.0
    total_commissions = 0.0
    total_funding = 0.0
    event_count = 0

    with open(events_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try:
                ev = json.loads(line)
                event_count += 1
            except json.JSONDecodeError:
                print(f"❌ ERROR: Corrupted JSON at line {line_num}")
                sys.exit(1)
                
            if ev.get("event_type") == "EXIT":
                total_gross_pnl += ev.get("pnl_usd", 0.0)
                total_commissions += ev.get("total_commission_usd", 0.0)
            elif ev.get("event_type") == "TP1_HIT":
                # ✅ طبق قرارداد، pnl_usd در TP1 جداگانه است و در EXIT کل سود جمع شده است
                total_commissions += ev.get("commission_usd", 0.0)
            elif ev.get("event_type") == "FUNDING":
                total_funding += ev.get("cashflow_usd", 0.0)

    if event_count == 0:
        print("⚠️ WARNING: Events file is empty. (Valid only for a newly started period)")

    calculated_equity = state['initial_equity'] + total_gross_pnl - total_commissions + total_funding

    print("-" * 50)
    print(f"Total Gross PnL (from EXIT): {total_gross_pnl:.4f}")
    print(f"Total Commissions: {total_commissions:.4f}")
    print(f"Total Funding: {total_funding:.4f}")
    print("-" * 50)
    print(f"Calculated Equity: {calculated_equity:.4f}")
    
    diff = abs(calculated_equity - state['equity'])
    if diff < 1e-5:
        print("✅ AUDIT PASSED: State equity matches event ledger.")
    else:
        print(f"❌ AUDIT FAILED: Difference of {diff:.4f} detected.")
        print("   This may be due to missing events or unrecorded commissions in old logs.")
        sys.exit(1)

if __name__ == "__main__":
    main()