"""
paper_probe.py — Scheduler probe برای Paper Trading persistence.

این اسکریپت در GitHub Actions اجرا می‌شود و هر بار:

  1. state را از STATE_DIR می‌خواند (یا init می‌کند)
  2. counter داخلی را در state افزایش می‌دهد
  3. یک HEARTBEAT event به events.jsonl اضافه می‌کند
  4. state را ذخیره می‌کند

هدف: تأیید persistence — که state بین اجراهای Actions
بازیابی می‌شود و events بدون تکرار اضافه می‌شوند.

هیچ معامله‌ای انجام نمی‌شود. هیچ API خصوصی صدا زده نمی‌شود.
فقط heartbeat است.

متغیر محیطی:
  STATE_DIR : مسیر دایرکتوری paper-state (پیش‌فرض: ./paper_state_dir)

Spec: docs/step9a_paper_persistence.md
"""

import os
import sys

from paper_state_manager import (
    init_state,
    load_state,
    save_state,
    state_exists,
    append_events,
    load_events,
    events_count,
)
from compute_hash import compute_strategy_hash


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def main():
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")

    print("=" * 60)
    print("🔎 PAPER PROBE — Persistence Verification")
    print("=" * 60)
    print(f"  STATE_DIR  : {state_dir}")
    print(f"  strategy   : {compute_strategy_hash()}")
    print("=" * 60)
    print()

    # ---- 1. State: load or init ----
    was_existing = state_exists(state_dir)
    if was_existing:
        state = load_state(state_dir)
        print(f"✅ State loaded from {state_dir}")
    else:
        state = init_state(state_dir, initial_equity=1000.0,
                           symbol="BTC-SWAP-USDT")
        print(f"✅ State initialized")

    # ---- 2. Increment counter ----
    prev_count = state.get("probe_counter", 0)
    new_count = prev_count + 1
    state["probe_counter"] = new_count
    state["probe_last_run_ts_ms"] = _now_ms()

    print(f"  probe_counter : {prev_count} → {new_count}")
    print(f"  equity        : {state['equity']}")
    print(f"  peak_equity   : {state['peak_equity']}")
    print()

    # ---- 3. Heartbeat event ----
    heartbeat = {
        "type": "HEARTBEAT",
        "timestamp": state["probe_last_run_ts_ms"],
        "counter": new_count,
        "equity_at_heartbeat": state["equity"],
    }
    n_app, n_sk = append_events(state_dir, [heartbeat])
    print(f"  heartbeat event: appended={n_app} skipped={n_sk}")
    print()

    # ---- 4. Save state ----
    save_state(state_dir, state)
    print(f"✅ State saved to {state_dir}")
    print()

    # ---- 5. Summary ----
    total_events = events_count(state_dir)
    all_events = load_events(state_dir)

    print("=" * 60)
    print("📌 SUMMARY")
    print("=" * 60)
    print(f"  run_number         : {new_count}")
    print(f"  total events       : {total_events}")
    print(f"  last event type    : "
          f"{all_events[-1]['event_type'] if all_events else 'N/A'}")
    print(f"  last event seq     : "
          f"{all_events[-1]['seq'] if all_events else 'N/A'}")
    print(f"  state file         : exists")
    print(f"  events file        : exists")
    print("=" * 60)
    print("✅ PAPER PROBE COMPLETED")


if __name__ == "__main__":
    main()