"""
run_mock_live.py — Mock Live Orchestrator for ApexQuant.
v3.1 - Strict three-state diagnostic monitor.
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
    initialize_event_baseline,
    notify_pending_events,
    notify_run_status,
    notify_strategy_monitor,
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
    return compute_all(
        klines, ema_fast_p=p["ema_fast_p"], ema_slow_p=p["ema_slow_p"],
        donchian_p=p["donchian_p"], atr_p=p["atr_p"],
    )


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
    ks_reason = "fail-closed: kill_switch check did not complete"
    try:
        result = is_kill_switch_active(state_dir, state=state, repo_root=repo_root)
        if not isinstance(result, tuple) or len(result) != 2:
            ks_active = True; ks_reason = "fail-closed: invalid kill_switch output"
        else:
            active_val, reason_val = result
            if not isinstance(active_val, bool):
                ks_active = True; ks_reason = "fail-closed: kill_switch active flag not bool"
            elif active_val:
                ks_active = True; ks_reason = reason_val if reason_val is not None else "kill_switch active"
            else:
                ks_active = False; ks_reason = None
    except Exception as e:
        ks_active = True; ks_reason = f"fail-closed: kill_switch raised {type(e).__name__}: {e}"
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
    if intent is None:
        raise ValueError(f"Missing 'intent' in event: {ev}")

    order_intent = "EXIT" if intent == "TP1_HIT" else intent

    qty = ev.get("qty")
    if qty is None:
        raise ValueError(f"Missing 'qty' in event: {ev}")

    price = ev.get("price", 0.0)

    req = OrderRequest(
        symbol=symbol, side=ev["side"], order_type="MARKET",
        qty=qty, price=None, leverage=leverage, intent=order_intent
    )
    return req, price, intent


def _build_strategy_monitor(indicators, signal, index, symbol, interval, monitor_status):
    """Build a read-only diagnostic reflecting exact strategy conditions."""
    closes = indicators["closes"]
    ema_fast = indicators["ema_fast"]
    ema_slow = indicators["ema_slow"]
    donchian_high = indicators["donchian_high"]
    donchian_low = indicators["donchian_low"]
    timestamps = indicators["timestamps"]

    result = {
        "symbol": symbol, "interval": interval, "monitor_status": monitor_status,
        "signal": signal.get("signal", "UNKNOWN"), "reason": signal.get("reason", "N/A"),
        "candle_time_utc": "N/A", "close": None, "ema_fast": None, "ema_slow": None,
        "ema_alignment": "UNAVAILABLE", "close_vs_ema_slow": "UNAVAILABLE",
        "donchian_high_prev": None, "donchian_low_prev": None, "breakout": "UNAVAILABLE",
        "conditions": {}
    }

    if index < 0 or index >= len(closes):
        return result

    ts = timestamps[index]
    result["candle_time_utc"] = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    close = closes[index]
    fast = ema_fast[index]
    slow = ema_slow[index]
    result["close"] = close
    result["ema_fast"] = fast
    result["ema_slow"] = slow

    # --- ارزیابی دقیق شروط (سه‌حالته: True/False/None) ---
    conds = {}
    if fast is not None and slow is not None and close is not None:
        conds["ema_bullish"] = (fast > slow)
        conds["ema_bearish"] = (fast < slow)
        conds["price_above_ema200"] = (close > slow)
        conds["price_below_ema200"] = (close < slow)
        
        if fast > slow: result["ema_alignment"] = "BULLISH"
        elif fast < slow: result["ema_alignment"] = "BEARISH"
        else: result["ema_alignment"] = "EQUAL"
        
        if close > slow: result["close_vs_ema_slow"] = "ABOVE"
        elif close < slow: result["close_vs_ema_slow"] = "BELOW"
        else: result["close_vs_ema_slow"] = "EQUAL"
    else:
        conds["ema_bullish"] = None
        conds["ema_bearish"] = None
        conds["price_above_ema200"] = None
        conds["price_below_ema200"] = None

    if index >= 1:
        dh = donchian_high[index - 1]
        dl = donchian_low[index - 1]
        result["donchian_high_prev"] = dh
        result["donchian_low_prev"] = dl

        if dh is not None and dl is not None and close is not None:
            conds["breakout_up"] = (close > dh)
            conds["breakdown_down"] = (close < dl)
            if close > dh: result["breakout"] = "UP"
            elif close < dl: result["breakout"] = "DOWN"
            else: result["breakout"] = "NONE"
        else:
            conds["breakout_up"] = None
            conds["breakdown_down"] = None
    else:
        conds["breakout_up"] = None
        conds["breakdown_down"] = None

    # نتیجه نهایی منطقی
    if conds.get("ema_bullish") and conds.get("price_above_ema200") and conds.get("breakout_up"):
        conds["final_long"] = True
    else:
        conds["final_long"] = False
        
    if conds.get("ema_bearish") and conds.get("price_below_ema200") and conds.get("breakdown_down"):
        conds["final_short"] = True
    else:
        conds["final_short"] = False

    result["conditions"] = conds
    return result


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
    strategy_monitor = None

    em = ExecutionManager(mode="MOCK_LIVE", client=None)

    def execution_callback(pending_info):
        req, ref_price, original_intent = _create_order_from_event(
            pending_info, symbol, engine_config.get("leverage", 1)
        )

        validation_state = copy.deepcopy(state)

        if req.intent == "ENTRY":
            if validation_state.get("pending_entry"):
                if validation_state["pending_entry"].get("signal_candle_ts") == pending_info.get("signal_candle_ts"):
                    validation_state["pending_entry"] = None
                else:
                    print(f"    ⛔ [MOCK_LIVE] {original_intent} REJECTED: Stale pending_entry.")
                    return False

        decision = em.execute(req, specs, validation_state, engine_config, ref_price=ref_price)

        if decision.accepted:
            print(f"    🛡️ [MOCK_LIVE] {original_intent} ACCEPTED. Size: {req.qty}")
            return True
        else:
            print(f"    ⛔ [MOCK_LIVE] {original_intent} REJECTED: {decision.reason}")
            return False

    try:
        print("=" * 70)
        print("🛡️  MOCK LIVE TRADING RUN (STRICT MONITOR)")
        print("=" * 70)
        print(f"  STATE_DIR : {state_dir}")
        print(f"  SYMBOL    : {symbol}")
        print(f"  INTERVAL  : {interval}")
        print(f"  strategy  : {compute_strategy_hash()}")
        print("=" * 70)
        print()

        was_existing = state_exists(state_dir)
        if was_existing:
            try:
                state = load_state(state_dir)
                print("✅ State loaded (existing)")
            except ValueError as e:
                print(f"❌ State hash mismatch: {e}")
                raise _RunComplete(1, "aborted", str(e))
        else:
            state = init_state(state_dir, initial_equity=1000.0, symbol=symbol)
            print("✅ State initialized")

        last_processed = state.get("last_processed_candle_ts")
        equity = state["equity"]
        peak_equity = state["peak_equity"]
        has_position = state["position"] is not None

        if not initialize_event_baseline(state_dir):
            print("⚠️ Telegram event baseline unavailable; notifications paused.")
        else:
            sent = notify_pending_events(state_dir)
            if sent:
                print(f"📨 Telegram confirmed {sent} pending notification(s) on startup.")

        print(f"  last_processed_candle_ts : {last_processed}")
        print(f"  equity                   : {equity}")
        print(f"  has_position             : {has_position}")
        print()

        print("📋 Fetching symbol specs...")
        specs = get_symbol_specs(symbol)

        print(f"📥 Fetching closed {interval} candles...")
        klines = fetch_closed_klines(symbol, interval, limit=300)

        if len(klines) < 250:
            print(f"⚠️  کندل کافی نیست ({len(klines)} < 250) — exit")
            raise _RunComplete(0, "insufficient_data")

        print("🧠 Computing indicators...")
        indicators = _build_indicators(klines)
        new_indices = _find_new_candles(klines, last_processed)

        # ✅ Preview last candle for diagnostics
        latest_idx = len(klines) - 1
        preview = evaluate_signal(indicators, latest_idx)

        if not new_indices:
            print(f"⏭  No new closed candle. Last = {last_processed}")
            strategy_monitor = _build_strategy_monitor(
                indicators=indicators, signal=preview, index=latest_idx,
                symbol=symbol, interval=interval, monitor_status="NO_NEW_CANDLES"
            )
            raise _RunComplete(0, "no_op")

        total_new = len(new_indices)
        if total_new > MAX_CATCHUP_CANDLES:
            to_process = new_indices[:MAX_CATCHUP_CANDLES]
            catchup_lag = total_new - MAX_CATCHUP_CANDLES
        else:
            to_process = new_indices
            catchup_lag = 0

        print("🔍 Reconciler check...")
        now_ms = int(time.time() * 1000)
        rec = reconcile_with_data(state, klines, interval, now_ms)
        reconciler_status = rec["status"]
        reconciler_summary = rec["summary"]
        if reconciler_status == "ERROR":
            print(f"❌ reconciler reported ERROR: {reconciler_summary}")
            raise _RunComplete(1, "aborted", f"reconciler: {reconciler_summary}")

        engine_config = _build_engine_config_with_kill_switch(specs, state, state_dir, repo_root=os.getcwd())
        strategy_hash = compute_strategy_hash()

        print(f"⚙️  Processing {len(to_process)} candle(s)...")
        total_events_generated = 0

        for n, idx in enumerate(to_process, start=1):
            candle_ts = int(klines[idx][0])
            sig = evaluate_signal(indicators, idx)
            signal_label = sig.get("signal", "NO_TRADE")

            # ✅ Update monitor for actual processed candle
            strategy_monitor = _build_strategy_monitor(
                indicators=indicators, signal=sig, index=idx,
                symbol=symbol, interval=interval, monitor_status="SUCCESS"
            )

            try:
                state, events = process_candle(
                    state, klines, indicators, idx, specs, engine_config, 
                    signal=sig, execution_callback=execution_callback, exit_callback=execution_callback
                )
            except Exception as e:
                print(f"  [{n}/{len(to_process)}] → ERROR in engine: {type(e).__name__}: {e}")
                traceback.print_exc()
                raise _RunComplete(1, "failed", f"paper_engine at idx={idx}: {type(e).__name__}: {e}")

            if events:
                try:
                    appended, skipped = append_events(state_dir, events, strategy_hash=strategy_hash)
                    save_state(state_dir, state)
                    total_events_generated += appended
                except Exception as e:
                    print(f"  [{n}/{len(to_process)}] → ERROR in append/save: {type(e).__name__}: {e}")
                    traceback.print_exc()
                    raise _RunComplete(1, "failed", f"save failed at idx={idx}: {type(e).__name__}: {e}")
            else:
                try:
                    save_state(state_dir, state)
                except Exception as e:
                    print(f"  [{n}/{len(to_process)}] → ERROR in save_state: {type(e).__name__}: {e}")
                    traceback.print_exc()
                    raise _RunComplete(1, "failed", f"save_state at idx={idx}: {type(e).__name__}: {e}")

            try:
                sent = notify_pending_events(state_dir)
                if sent:
                    print(f"  📨 Telegram confirmed {sent} event notification(s).")
            except Exception as e:
                print(f"  ⚠️ Telegram notification issue: {type(e).__name__}")

            candles_processed += 1
            print(f"  [{n}/{len(to_process)}] idx={idx} ts={candle_ts} signal={signal_label} events={len(events)}")

        equity = state["equity"]
        peak_equity = state["peak_equity"]
        has_position = state["position"] is not None
        last_processed = state.get("last_processed_candle_ts")
        total_events_count = events_count(state_dir)

        print("=" * 70)
        print("📌 MOCK LIVE RUN SUMMARY")
        print(f"  candles_processed  : {candles_processed}")
        print(f"  catchup_lag        : {catchup_lag}")
        print(f"  events_generated   : {total_events_generated}")
        print(f"  total_events       : {total_events_count}")
        print(f"  equity             : {equity}")
        print(f"  has_position       : {has_position}")
        print("=" * 70)
        print("✅ MOCK LIVE RUN COMPLETED")

        run_status = "success"
        exit_code = 0

    except _RunComplete as rc:
        exit_code = rc.exit_code
        run_status = rc.run_status
        error_message = rc.error_message

        if exit_code != 0:
            print(f"\n❌ RUN FAILED: {error_message}")
        else:
            print(f"\n⏸️ RUN FINISHED: {run_status}")
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
            print(f"\n📝 paper_status.json written (run_status={run_status})")
        except Exception as we:
            print(f"\n❌ failed to write paper_status.json: {type(we).__name__}: {we}")
            if exit_code == 0: exit_code = 1

        try:
            if not notify_run_status(status):
                print("⚠️ Telegram status notification was not confirmed.")
        except Exception:
            pass

        if strategy_monitor is not None:
            try:
                if not notify_strategy_monitor(strategy_monitor):
                    print("⚠️ Strategy monitor notification was not confirmed.")
            except Exception:
                print("⚠️ Strategy monitor notification failed.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())