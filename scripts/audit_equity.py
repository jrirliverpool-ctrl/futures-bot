"""
audit_equity.py — v3.0 Strict Equity Audit.
Fails if:
  - State/Events missing or corrupt.
  - Open position exists (unrealized PnL cannot be reconciled).
  - Empty ledger without a valid period_start_utc marker.
  - Equity does not match ledger.
"""
import json
import os
import sys

def main():
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    state_path = os.path.join(state_dir, "state.json")
    events_path = os.path.join(state_dir, "events.jsonl")

    # 1. بررسی وجود State
    if not os.path.exists(state_path):
        print("❌ AUDIT FAILED: state.json not found.")
        sys.exit(1)
        
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
    except json.JSONDecodeError:
        print("❌ AUDIT FAILED: state.json is corrupted.")
        sys.exit(1)

    print("=" * 50)
    print("EQUITY AUDIT v3.0 (STRICT)")
    print("=" * 50)
    print(f"Current Equity: {state['equity']}")
    print(f"Initial Equity: {state['initial_equity']}")
    print(f"Period Start UTC: {state.get('period_start_utc', 'N/A')}")

    # 2. بررسی پوزیشن باز (عدم امکان حسابرسی دقیق)
    if state.get("position") is not None:
        print("\n❌ AUDIT FAILED: Open position exists.")
        print("   Cannot verify equity against realized ledger while a position is open.")
        print("   Wait for the position to close before running this audit.")
        sys.exit(1)
    print("✅ No open position (all PnL is realized).")

    # 3. بررسی فایل Events
    if not os.path.exists(events_path):
        print("❌ AUDIT FAILED: events.jsonl not found.")
        sys.exit(1)

    file_size = os.path.getsize(events_path)
    if file_size == 0:
        # حالت دفتر خالی: فقط اگر مارکر دوره جدید وجود داشته باشد مجاز است
        if state.get("period_start_utc"):
            if abs(state['equity'] - state['initial_equity']) < 1e-9:
                print(f"\n✅ AUDIT PASSED: Fresh period verified.")
                print(f"   Period Start: {state['period_start_utc']}")
                print(f"   Base Equity: {state['initial_equity']}")
                print("   Ledger is empty, but period marker is valid.")
                sys.exit(0)
            else:
                print("\n❌ AUDIT FAILED: Empty ledger but Equity != Initial Equity.")
                sys.exit(1)
        else:
            print("\n❌ AUDIT FAILED: events.jsonl is empty AND no period_start_utc marker.")
            print("   This means either the file is missing/corrupted, or a new period was never explicitly started.")
            sys.exit(1)

    # 4. پردازش رویدادها (حسابرسی عادی)
    total_gross_pnl = 0.0
    total_commissions = 0.0
    total_funding = 0.0
    event_count = 0
    exit_count = 0

    with open(events_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try:
                ev = json.loads(line)
                event_count += 1
            except json.JSONDecodeError:
                print(f"❌ AUDIT FAILED: Corrupted JSON at line {line_num}")
                sys.exit(1)
                
            if ev.get("event_type") == "EXIT":
                exit_count += 1
                total_gross_pnl += ev.get("pnl_usd", 0.0)
                # total_commission_usd شامل entry + TP1 + final است
                total_commissions += ev.get("total_commission_usd", 0.0)
            elif ev.get("event_type") == "FUNDING":
                total_funding += ev.get("cashflow_usd", 0.0)

    calculated_equity = state['initial_equity'] + total_gross_pnl - total_commissions + total_funding

    print("-" * 50)
    print(f"Events: {event_count} (EXIT events: {exit_count})")
    print(f"Total Gross PnL: {total_gross_pnl:.4f}")
    print(f"Total Commissions: {total_commissions:.4f}")
    print(f"Total Funding: {total_funding:.4f}")
    print(f"Calculated Equity: {calculated_equity:.4f}")
    print(f"Current Equity: {state['equity']:.4f}")
    
    diff = abs(calculated_equity - state['equity'])
    if diff < 1e-5:
        print("\n✅ AUDIT PASSED: State equity matches event ledger.")
        sys.exit(0)
    else:
        print(f"\n❌ AUDIT FAILED: Difference of {diff:.4f} detected.")
        sys.exit(1)

if __name__ == "__main__":
    main()