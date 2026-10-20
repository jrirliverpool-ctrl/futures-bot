"""
run_mock_live.py — Mock Live Orchestrator for ApexQuant.
v1.3 - Validate-Before-Commit + Pending Entry Isolation.
"""

import json
import os
import sys
import tempfile
import time
import copy
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

# ✅ Importهای لایه اجرا
from execution_manager import ExecutionManager
from order_executor import OrderRequest


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
        klines,
        ema_fast_p=p["ema_fast_p"],
        ema_slow_p=p["ema_slow_p"],
        donchian_p=p["donchian_p"],
        atr_p=p["atr_p"],
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
            ks_active = True
            ks_reason = "fail-closed: invalid kill_switch output"
        else:
            active_val, reason_val = result
            if not isinstance(active_val, bool):
                ks_active = True
                ks_reason = f"fail-closed: kill_switch active flag not bool (got {type(active_val).__name__})"
            elif active_val:
                ks_active = True
                ks_reason = reason_val if reason_val is not None else "kill_switch active (no reason provided)"
            else:
                ks_active = False
                ks_reason = None
    except Exception as e:
        ks_active = True
        ks_reason = f"fail-closed: kill_switch raised {type(e).__name__}: {e}"
    cfg["kill_switch_active"] = ks_active
    cfg["kill_switch_reason"] = ks_reason
    return cfg


def _find_new_candles(klines, last_processed_ts):
    if not klines:
        return []
    if last_processed_ts is None:
        return list(range(len(klines)))
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


def main() -> int:
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")
    start_iso = datetime.now(timezone.utc).isoformat()

    # ✅ مقادیر پیش‌فرض برای جلوگیری از خطا در finally
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

    em = ExecutionManager(mode="MOCK_LIVE", client=None)

    try:
        print("=" * 70)
        print("🛡️  MOCK LIVE TRADING RUN (VALIDATE-BEFORE-COMMIT)")
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

        new_indices = _find_new_candles(klines, last_processed)
        if not new_indices:
            print(f"⏭  No new closed candle. Last = {last_processed}")
            raise _RunComplete(0, "no_op")

        total_new = len(new_indices)
        if total_new > MAX_CATCHUP_CANDLES:
            to_process = new_indices[:MAX_CATCHUP_CANDLES]
            catchup_lag = total_new - MAX_CATCHUP_CANDLES
        else:
            to_process = new_indices
            catchup_lag = 0

        print("🧠 Computing indicators...")
        indicators = _build_indicators(klines)

        print("🔍 Reconciler check...")
        now_ms = int(time.time() * 1000)
        rec = reconcile_with_data(state, klines, interval, now_ms)
        reconciler_status = rec["status"]
        reconciler_summary = rec["summary"]
        if reconciler_status == "ERROR":
            print(f"❌ reconciler reported ERROR: {reconciler_summary}")
            raise _RunComplete(1, "aborted", f"reconciler: {reconciler_summary}")

        engine_config = _build_engine_config_with_kill_switch(specs, state, state_dir, repo_root=os.getcwd())

        print(f"⚙️  Processing {len(to_process)} candle(s)...")
        total_events_generated = 0

        for n, idx in enumerate(to_process, start=1):
            candle_ts = int(klines[idx][0])
            sig = evaluate_signal(indicators, idx)
            signal_label = sig.get("signal", "NO_TRADE")

            # ✅ ۱. شبیه‌سازی روی یک کپی
            simulated_state = copy.deepcopy(state)
            
            try:
                simulated_state, events = process_candle(
                    simulated_state, klines, indicators, idx, specs, engine_config, signal=sig,
                )
            except Exception as e:
                print(f"  [{n}/{len(to_process)}] → ERROR in engine: {type(e).__name__}: {e}")
                raise _RunComplete(1, "failed", f"paper_engine at idx={idx}: {type(e).__name__}: {e}")

            entry_events = [e for e in events if e["type"] == "ENTRY"]
            execution_rejected = False

            if entry_events:
                ev = entry_events[0]
                req = OrderRequest(
                    symbol=symbol,
                    side=ev["side"],
                    order_type="MARKET",
                    qty=ev["size"],
                    price=None,
                    leverage=1, # ✅ اهرم محافظه‌کارانه
                    intent="ENTRY"
                )
                
                # ✅ ۲. برای جلوگیری از گیت duplicate اشتباه، pending_entry را در state اصلی پاک می‌کنیم
                validation_state = copy.deepcopy(state)
                validation_state["pending_entry"] = None
                
                decision = em.execute(req, specs, validation_state, engine_config, ref_price=ev["price"])
                
                if not decision.accepted:
                    print(f"    ⛔ [MOCK_LIVE] Entry REJECTED: {decision.reason}")
                    execution_rejected = True
                else:
                    print(f"    🛡️ [MOCK_LIVE] Entry ACCEPTED. Size: {req.qty}")

            # ✅ ۳. تصمیم‌گیری نهایی
            if execution_rejected:
                # وضعیت اصلی را دست نمی‌زنیم، فقط زمان پردازش را جلو می‌بریم
                state["last_processed_candle_ts"] = candle_ts
                state["catchup_lag_candles"] = catchup_lag
            else:
                # وضعیت شبیه‌سازی‌شده را جایگزین می‌کنیم
                state = simulated_state
                state["last_processed_candle_ts"] = candle_ts
                state["symbol"] = symbol
                state["contract_multiplier"] = specs["contract_multiplier"]
                state["last_close_price"] = float(klines[idx][4])
                state["catchup_lag_candles"] = catchup_lag

            # ✅ ۴. ذخیره State (قبل از ثبت Events)
            try:
                save_state(state_dir, state)
            except Exception as e:
                raise _RunComplete(1, "failed", f"save_state at idx={idx}: {type(e).__name__}: {e}")

            # ✅ ۵. ثبت Events (فقط اگر State ذخیره شده باشد و سفارش رد نشده باشد)
            if not execution_rejected and events:
                append_events(state_dir, events)
                total_events_generated += len(events)

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
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        print(f"❌ FATAL: {error_message}")
        run_status = "failed"
        exit_code = 1
    finally:
        status = {
            "schema_version": PAPER_STATUS_SCHEMA,
            "run_started_at_iso": start_iso,
            "run_finished_at_iso": datetime.now(timezone.utc).isoformat(),
            "run_status": run_status,
            "run_exit_code": exit_code,
            "strategy_hash": compute_strategy_hash(),
            "state_dir": state_dir,
            "symbol": symbol,
            "interval": interval,
            "reconciler_status": reconciler_status,
            "reconciler_summary": reconciler_summary,
            "last_processed_candle_ts": last_processed,
            "candles_processed_this_run": candles_processed,
            "catchup_lag": catchup_lag,
            "total_new_detected": total_new,
            "equity": equity,
            "peak_equity": peak_equity,
            "has_position": has_position,
            "error_message": error_message,
        }
        try:
            _write_paper_status_atomic(state_dir, status)
            print(f"\n📝 paper_status.json written (run_status={run_status})")
        except Exception as we:
            print(f"\n❌ failed to write paper_status.json: {type(we).__name__}: {we}")
            if exit_code == 0:
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())