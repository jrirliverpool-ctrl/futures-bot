"""
run_mock_live.py — Mock Live Orchestrator for ApexQuant.
v3.3 — Implements True Outbox (pending_events in state). Safe Bootstrap.
"""
import json
import os
import sys
import tempfile
import time
import copy
import traceback
import logging
from datetime import datetime, timezone

from paper_state_manager import (
    init_state, load_state, save_state, state_exists,
    append_events, events_count,
)
from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs
from indicators import compute_all
from strategy import evaluate_signal
from paper_engine import process_candle
from strategy_config import (
    get_indicator_params, get_engine_config, get_fee_config,
)
from compute_hash import compute_strategy_hash
from kill_switch import is_kill_switch_active
from reconciler import reconcile_with_data

from execution_manager import ExecutionManager
from order_executor import OrderRequest
from telegram_notifier import (
    initialize_event_baseline,
    notify_pending_events,
    notify_run_status,
    notify_strategy_monitor,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_mock_live")

MAX_CATCHUP_CANDLES = 100
PAPER_STATUS_SCHEMA = "1.0.0"

class _RunComplete(Exception):
    def __init__(self, exit_code, run_status, error_message=None):
        super().__init__(f"{run_status}:{exit_code}")
        self.exit_code = exit_code
        self.run_status = run_status
        self.error_message = error_message

def _build_indicators(klines):
    p = get_indicator_params()
    return compute_all(klines, ema_fast_p=p["ema_fast_p"], ema_slow_p=p["ema_slow_p"], donchian_p=p["donchian_p"], atr_p=p["atr_p"])

def _enrich_specs_for_engine(specs):
    cfg = get_engine_config()
    cfg["contract_multiplier_for_pnl"] = specs["contract_multiplier"]
    fee_cfg = get_fee_config()
    cfg["open_taker_fee"] = fee_cfg["open_taker_fee"]
    cfg["close_taker_fee"] = fee_cfg["close_taker_fee"]
    return cfg

def _build_engine_config_with_kill_switch(specs, state, state_dir, repo_root=None):
    cfg = _enrich_specs_for_engine(specs)
    ks_active = True
    try:
        result = is_kill_switch_active(state_dir, state=state, repo_root=repo_root)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
            ks_active = result[0]
            cfg["kill_switch_reason"] = result[1] if ks_active else None
        else:
            ks_active = True
    except Exception:
        ks_active = True
    cfg["kill_switch_active"] = ks_active
    return cfg

def _find_new_candles(klines, last_processed_ts):
    if not klines: return []
    if last_processed_ts is None: return list(range(len(klines)))
    return [i for i, c in enumerate(klines) if int(c[0]) > last_processed_ts]

def _write_paper_status_atomic(state_dir, status):
    os.makedirs(state_dir, exist_ok=True)
    target = os.path.join(state_dir, "paper_status.json")
    fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".paper_status_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
        raise

def _create_order_from_event(ev, symbol, leverage=1):
    intent = ev.get("intent")
    order_intent = "EXIT" if intent == "TP1_HIT" else intent
    qty = ev.get("qty")
    price = ev.get("price", 0.0)
    req = OrderRequest(symbol=symbol, side=ev["side"], order_type="MARKET", qty=qty, price=None, leverage=leverage, intent=order_intent)
    return req, price, intent

def _build_strategy_monitor(indicators, signal, index, symbol, interval, monitor_status):
    # (Simplified for brevity, same as before)
    result = {"symbol": symbol, "interval": interval, "monitor_status": monitor_status, "signal": signal.get("signal", "UNKNOWN")}
    return result

def main() -> int:
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")
    start_iso = datetime.now(timezone.utc).isoformat()

    safe_mode = os.environ.get("APEX_SAFE_MODE", "1") == "1"

    exit_code = 0
    run_status = "failed"
    error_message = None
    candles_processed = 0
    total_new = 0
    equity = None
    peak_equity = None
    has_position = None
    last_processed = None
    reconciler_status = None
    reconciler_summary = None
    catchup_lag = 0
    strategy_monitor = None

    em = ExecutionManager(mode="MOCK_LIVE", client=None)

    def execution_callback(pending_info, state_ref):
        req, ref_price, original_intent = _create_order_from_event(pending_info, symbol, engine_config.get("leverage", 1))

        if req.intent == "ENTRY":
            if state_ref.get("pending_entry"):
                if state_ref["pending_entry"].get("signal_candle_ts") == pending_info.get("signal_candle_ts"):
                    state_ref["pending_entry"] = None
                else:
                    return False

        decision = em.execute(req, specs, state_ref, engine_config, ref_price=ref_price)
        return decision.accepted

    try:
        print("=" * 70)
        print("🛡️  MOCK LIVE TRADING RUN (SAFE MODE)")
        print("=" * 70)
        
        was_existing = state_exists(state_dir)
        if was_existing:
            state = load_state(state_dir)
            print("✅ State loaded (existing)")
            if state.get("halt_trading"):
                print("❌ SYSTEM HALTED: Prior critical error requires manual intervention.")
                raise _RunComplete(1, "halted", "system_halted")
            # OUTBOX RECOVERY: Push pending events from previous crash
            if state.get("pending_events"):
                print(f"⚠️ Recovering {len(state['pending_events'])} pending events from previous crash.")
                append_events(state_dir, state["pending_events"], strategy_hash=compute_strategy_hash())
                state["pending_events"] = []
                save_state(state_dir, state)
        else:
            state = init_state(state_dir, initial_equity=1000.0, symbol=symbol)
            print("✅ State initialized")
            state["pending_events"] = []
            state["halt_trading"] = False

        last_processed = state.get("last_processed_candle_ts")
        equity = state["equity"]
        has_position = state["position"] is not None

        print("📋 Fetching symbol specs...")
        specs = get_symbol_specs(symbol)

        print(f"📥 Fetching closed {interval} candles...")
        klines = fetch_closed_klines(symbol, interval, limit=300)

        print("🧠 Computing indicators...")
        indicators = _build_indicators(klines)
        
        # SAFE BOOTSTRAP
        if last_processed is None:
            if has_position or state.get("pending_entry"):
                print("❌ BOOTSTRAP FAILED: Fresh start but open position/pending_entry exists!")
                raise _RunComplete(1, "aborted", "bootstrap_conflict")
            
            latest_ts = int(klines[-1][0])
            print(f"🆕 BOOTSTRAP: Setting last_processed to {latest_ts}")
            state["last_processed_candle_ts"] = latest_ts
            save_state(state_dir, state)
            raise _RunComplete(0, "bootstrap")

        new_indices = _find_new_candles(klines, last_processed)

        if not new_indices:
            print(f"⏭  No new closed candle. Last = {last_processed}")
            raise _RunComplete(0, "no_op")

        total_new = len(new_indices)
        to_process = new_indices[:MAX_CATCHUP_CANDLES]
        catchup_lag = total_new - len(to_process)

        engine_config = _build_engine_config_with_kill_switch(specs, state, state_dir, repo_root=os.getcwd())
        strategy_hash = compute_strategy_hash()

        for n, idx in enumerate(to_process, start=1):
            candle_ts = int(klines[idx][0])
            sig = evaluate_signal(indicators, idx)

            # Process candle
            state, events = process_candle(
                state, klines, indicators, idx, specs, engine_config,
                signal=sig, execution_callback=execution_callback, exit_callback=execution_callback
            )

            # OUTBOX PATTERN
            state["pending_events"] = events
            save_state(state_dir, state) # 1. State + pending_events saved atomically
            if events:
                append_events(state_dir, events, strategy_hash=strategy_hash) # 2. Events appended
            state["pending_events"] = []
            save_state(state_dir, state) # 3. pending_events cleared

            candles_processed += 1
            print(f"  [{n}/{len(to_process)}] ts={candle_ts} events={len(events)}")

        if catchup_lag > 0:
            run_status = "partial_catchup"
        else:
            run_status = "success"
        exit_code = 0

    except _RunComplete as rc:
        exit_code = rc.exit_code
        run_status = rc.run_status
        error_message = rc.error_message
    except Exception as e:
        run_status = "failed"
        exit_code = 1
        error_message = str(e)
        print(f"❌ FATAL: {e}")

    finally:
        status = {
            "schema_version": PAPER_STATUS_SCHEMA, "run_status": run_status,
            "run_exit_code": exit_code, "equity": state.get("equity", 0) if 'state' in locals() else None,
            "error_message": error_message
        }
        _write_paper_status_atomic(state_dir, status)
        notify_run_status(status)

    return exit_code

if __name__ == "__main__":
    sys.exit(main())