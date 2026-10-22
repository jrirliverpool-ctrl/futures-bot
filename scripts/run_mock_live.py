"""
run_mock_live.py — Mock Live Orchestrator for ApexQuant.
v2.9 - Atomic order: append_events then save_state.
"""
import json
import os
import sys
import tempfile
import time
import copy
import traceback
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
    initialize_event_baseline, notify_pending_events, notify_run_status,
)

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
    ks_reason = "fail-closed"
    try:
        result = is_kill_switch_active(state_dir, state=state, repo_root=repo_root)
        if isinstance(result, tuple) and len(result) == 2:
            ks_active, ks_reason = result
    except Exception as e:
        pass
    cfg["kill_switch_active"] = ks_active
    cfg["kill_switch_reason"] = ks_reason
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
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
        raise

def _create_order_from_event(ev, symbol, leverage=1):
    intent = ev.get("intent")
    if intent is None: raise ValueError("Missing intent")
    order_intent = "EXIT" if intent == "TP1_HIT" else intent
    qty = ev.get("qty")
    if qty is None: raise ValueError("Missing qty")
    price = ev.get("price", 0.0)
    req = OrderRequest(symbol=symbol, side=ev["side"], order_type="MARKET", qty=qty, price=None, leverage=leverage, intent=order_intent)
    return req, price, intent

def main() -> int:
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")
    start_iso = datetime.now(timezone.utc).isoformat()

    exit_code = 0; run_status = "failed"; error_message = None
    candles_processed = 0; total_new = 0
    equity = None; peak_equity = None; has_position = None
    last_processed = None; reconciler_status = None; reconciler_summary = None
    catchup_lag = 0

    em = ExecutionManager(mode="MOCK_LIVE", client=None)

    def execution_callback(pending_info):
        req, ref_price, original_intent = _create_order_from_event(pending_info, symbol, engine_config.get("leverage", 1))
        validation_state = copy.deepcopy(state)
        if req.intent == "ENTRY":
            if validation_state.get("pending_entry"):
                if validation_state["pending_entry"].get("signal_candle_ts") == pending_info.get("signal_candle_ts"):
                    validation_state["pending_entry"] = None
                else:
                    return False
        decision = em.execute(req, specs, validation_state, engine_config, ref_price=ref_price)
        if decision.accepted:
            print(f"    🛡️ [MOCK_LIVE] {original_intent} ACCEPTED. Size: {req.qty}")
            return True
        else:
            print(f"    ⛔ [MOCK_LIVE] {original_intent} REJECTED: {decision.reason}")
            return False

    try:
        was_existing = state_exists(state_dir)
        if was_existing:
            state = load_state(state_dir)
        else:
            state = init_state(state_dir, initial_equity=1000.0, symbol=symbol)

        last_processed = state.get("last_processed_candle_ts")
        equity = state["equity"]
        peak_equity = state["peak_equity"]
        has_position = state["position"] is not None

        if not initialize_event_baseline(state_dir):
            print("⚠️ Telegram baseline unavailable.")
        else:
            sent = notify_pending_events(state_dir)
            if sent: print(f"📨 Telegram confirmed {sent} pending notifications.")

        specs = get_symbol_specs(symbol)
        klines = fetch_closed_klines(symbol, interval, limit=300)

        if len(klines) < 250:
            raise _RunComplete(0, "insufficient_data")

        new_indices = _find_new_candles(klines, last_processed)
        if not new_indices:
            raise _RunComplete(0, "no_op")

        total_new = len(new_indices)
        to_process = new_indices[:MAX_CATCHUP_CANDLES] if total_new > MAX_CATCHUP_CANDLES else new_indices
        catchup_lag = total_new - len(to_process) if total_new > MAX_CATCHUP_CANDLES else 0

        indicators = _build_indicators(klines)
        
        now_ms = int(time.time() * 1000)
        rec = reconcile_with_data(state, klines, interval, now_ms)
        reconciler_status = rec["status"]
        reconciler_summary = rec["summary"]
        if reconciler_status == "ERROR":
            raise _RunComplete(1, "aborted", f"reconciler: {reconciler_summary}")

        engine_config = _build_engine_config_with_kill_switch(specs, state, state_dir, repo_root=os.getcwd())
        strategy_hash = compute_strategy_hash()

        total_events_generated = 0

        for n, idx in enumerate(to_process, start=1):
            candle_ts = int(klines[idx][0])
            sig = evaluate_signal(indicators, idx)
            signal_label = sig.get("signal", "NO_TRADE")

            try:
                state, events = process_candle(state, klines, indicators, idx, specs, engine_config, signal=sig, execution_callback=execution_callback, exit_callback=execution_callback)
            except Exception as e:
                raise _RunComplete(1, "failed", f"paper_engine at idx={idx}: {type(e).__name__}: {e}")

            # ✅ ترتیب اتمیک: ابتدا ثبت رویدادها، سپس ذخیره وضعیت
            if events:
                try:
                    appended, skipped = append_events(state_dir, events, strategy_hash=strategy_hash)
                    save_state(state_dir, state)
                    total_events_generated += appended
                except Exception as e:
                    raise _RunComplete(1, "failed", f"save failed at idx={idx}: {type(e).__name__}: {e}")
            else:
                try:
                    save_state(state_dir, state)
                except Exception as e:
                    raise _RunComplete(1, "failed", f"save_state at idx={idx}: {type(e).__name__}: {e}")

            try:
                sent = notify_pending_events(state_dir)
                if sent: print(f"  📨 Telegram confirmed {sent} event notification(s).")
            except Exception as e:
                print(f"  ⚠️ Telegram notification issue: {type(e).__name__}")

            candles_processed += 1
            print(f"  [{n}/{len(to_process)}] idx={idx} ts={candle_ts} signal={signal_label} events={len(events)}")

        equity = state["equity"]
        peak_equity = state["peak_equity"]
        has_position = state["position"] is not None
        last_processed = state.get("last_processed_candle_ts")
        run_status = "success"
        exit_code = 0

    except _RunComplete as rc:
        exit_code = rc.exit_code
        run_status = rc.run_status
        error_message = rc.error_message
        print(f"\n{'❌ RUN FAILED' if exit_code != 0 else '⏸️ RUN FINISHED'}: {error_message}")
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        print(f"❌ FATAL: {error_message}")
        traceback.print_exc()
        run_status = "failed"; exit_code = 1
    finally:
        status = {
            "schema_version": PAPER_STATUS_SCHEMA,
            "run_started_at_iso": start_iso,
            "run_finished_at_iso": datetime.now(timezone.utc).isoformat(),
            "run_status": run_status, "run_exit_code": exit_code,
            "strategy_hash": compute_strategy_hash(),
            "state_dir": state_dir, "symbol": symbol, "interval": interval,
            "reconciler_status": reconciler_status, "reconciler_summary": reconciler_summary,
            "last_processed_candle_ts": last_processed,
            "candles_processed_this_run": candles_processed,
            "catchup_lag": catchup_lag, "total_new_detected": total_new,
            "equity": equity, "peak_equity": peak_equity, "has_position": has_position,
            "error_message": error_message,
        }
        try:
            _write_paper_status_atomic(state_dir, status)
        except Exception as we:
            if exit_code == 0: exit_code = 1
            status["run_exit_code"] = exit_code
            status["run_status"] = "failed"

        try:
            notify_run_status(status)
        except Exception:
            pass

    return exit_code

if __name__ == "__main__":
    sys.exit(main())