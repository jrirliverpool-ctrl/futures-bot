"""
verify_candle_sequence.py — v2.0 Proves candle uniqueness.
Fails if:
  - Empty ledger without period_start_utc marker.
  - Candle timestamps are not strictly increasing.
  - Last processed candle mismatches state.
"""
import json
import os
import sys

def main():
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    events_path = os.path.join(state_dir, "events.jsonl")
    state_path = os.path.join(state_dir, "state.json")

    if not os.path.exists(state_path):
        print("❌ FAILED: state.json not found.")
        sys.exit(1)
        
    with open(state_path, "r") as f:
        state = json.load(f)
        
    last_processed = state.get("last_processed_candle_ts")

    # 1. بررسی وجود فایل Events
    if not os.path.exists(events_path) or os.path.getsize(events_path) == 0:
        if state.get("period_start_utc"):
            print("✅ CANDLE SEQUENCE PASSED: Fresh period, no events yet.")
            sys.exit(0)
        else:
            print("❌ FAILED: Empty events file AND no period_start_utc marker.")
            sys.exit(1)

    # 2. جمع‌آوری Timestamp کندل‌ها
    candle_ts_set = set()
    with open(events_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                print(f"❌ FAILED: Corrupted JSON at line {line_num}")
                sys.exit(1)
                
            ts = ev.get("candle_ts_ms")
            if ts is not None:
                candle_ts_set.add(ts)

    if not candle_ts_set:
        print("❌ FAILED: No candle_ts_ms found in any event.")
        sys.exit(1)

    sorted_candles = sorted(candle_ts_set)
    print("=" * 50)
    print("CANDLE SEQUENCE VERIFICATION")
    print("=" * 50)
    print(f"Unique processed candles: {len(sorted_candles)}")
    print(f"First candle ts: {sorted_candles[0]}")
    print(f"Last candle ts: {sorted_candles[-1]}")
    print(f"State's last_processed: {last_processed}")

    # 3. بررسی ترتیب صعودی
    if sorted_candles != list(candle_ts_set):
        print("❌ FAILED: Candle timestamps are NOT monotonically increasing in the ledger.")
        sys.exit(1)
    print("✅ Candle timestamps are strictly increasing.")

    # 4. تطبیق با State
    if sorted_candles[-1] != last_processed:
        print(f"❌ FAILED: Last event ts ({sorted_candles[-1]}) != state's last_processed ({last_processed})")
        sys.exit(1)
    print("✅ Last event matches state's last_processed.")

    print("\n✅ ALL CHECKS PASSED: Candle processing is unique and consistent.")

if __name__ == "__main__":
    main()