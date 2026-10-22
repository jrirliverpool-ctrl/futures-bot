"""
start_new_period.py — Safely archives current state and starts a fresh 30-day period.
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone

def main():
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    state_path = os.path.join(state_dir, "state.json")
    events_path = os.path.join(state_dir, "events.jsonl")
    
    archive_dir = os.path.join(state_dir, "archive_v1")
    
    if not os.path.exists(state_path):
        print("❌ No active state to archive.")
        sys.exit(1)

    print("📦 Archiving current state...")
    os.makedirs(archive_dir, exist_ok=True)
    
    # کپی کردن فایل‌های فعلی به پوشه آرشیو
    if os.path.exists(state_path):
        shutil.copy2(state_path, os.path.join(archive_dir, "state.json"))
    if os.path.exists(events_path):
        shutil.copy2(events_path, os.path.join(archive_dir, "events.jsonl"))

    print("✅ Archive complete in paper_state_dir/archive_v1/")

    # خواندن State فعلی برای گرفتن هش و موجودی
    with open(state_path, "r") as f:
        old_state = json.load(f)

    current_equity = old_state["equity"]
    strategy_hash = old_state["strategy_hash"]
    last_ts = old_state.get("last_processed_candle_ts")

    # ساخت State جدید
    new_state = {
        "version": "1.0.0",
        "initial_equity": current_equity,
        "equity": current_equity,
        "peak_equity": current_equity,
        "symbol": old_state.get("symbol", "BTC-SWAP-USDT"),
        "position": None,
        "pending_entry": None,
        "last_processed_candle_ts": last_ts, # حفظ برای جلوگیری از پردازش مجدد
        "cooldown_until_index": None, # ریست کول‌داون
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "loss_day_key": None,
        "loss_week_key": None,
        "circuit_breaker_active": False,
        "last_funding_ts": None,
        "strategy_hash": strategy_hash,
        "strategy_version": "1.0.1",
        "period_start_utc": datetime.now(timezone.utc).isoformat()
    }

    # نوشتن State جدید (اتمیک)
    tmp_state = state_path + ".tmp"
    with open(tmp_state, "w") as f:
        json.dump(new_state, f, indent=2)
    os.replace(tmp_state, state_path)

    # خالی کردن Events
    with open(events_path, "w") as f:
        pass

    print("🚀 New 30-day period initialized.")
    print(f"   Base Equity: {current_equity}")
    print(f"   Strategy Hash: {strategy_hash}")
    print(f"   Start Time: {new_state['period_start_utc']}")

if __name__ == "__main__":
    main()