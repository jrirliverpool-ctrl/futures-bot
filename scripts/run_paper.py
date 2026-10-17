"""
run_paper.py — Paper Trading orchestrator برای ApexQuant.

v2.4 (B1 — Commission):
  - _enrich_specs_for_engine از get_fee_config() استفاده می‌کند
    (fail-loud — اگر get_fee_config خطا بدهد، exception بالا می‌رود)
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

from paper_state_manager import (
    init_state,
    load_state,
    save_state,
    state_exists,
    append_events,
    load_events,
    events_count,
)
from data_feed import fetch_closed_klines
from symbol_specs import get_symbol_specs
from indicators import compute_all
from strategy import evaluate_signal
from paper_engine import process_candle
from strategy_config import (
    get_indicator_params,
    get_engine_config,
    get_fee_config,
)
from compute_hash import compute_strategy_hash
from kill_switch import is_kill_switch_active
from reconciler import reconcile_with_data


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
    """
    v2.4 (B1): fail-loud اگر get_fee_config خطا بدهد.
    کارمزد صفر، فقط اگر آگاهانه در DEFAULT_CONFIG باشد،
    نه به‌خاطر خطای import.
    """
    cfg = get_engine_config()
    cfg["contract_multiplier_for_pnl"] = specs["contract_multiplier"]

    # B1: fail-loud — بدون try/except
    fee_cfg = get_fee_config()
    cfg["open_taker_fee"] = fee_cfg["open_taker_fee"]
    cfg["close_taker_fee"] = fee_cfg["close_taker_fee"]

    return cfg


def _build_engine_config_with_kill_switch(specs, state, state_dir,
                                           repo_root=None):
    cfg = _enrich_specs_for_engine(specs)

    ks_active = True
    ks_reason = "fail-closed: kill_switch check did not complete"

    try:
        result = is_kill_switch_active(
            state_dir, state=state, repo_root=repo_root,
        )

        if not isinstance(result, tuple) or len(result) != 2:
            ks_active = True
            ks_reason = "fail-closed: invalid kill_switch output"
        else:
            active_val, reason_val = result
            if not isinstance(active_val, bool):
                ks_active = True
                ks_reason = (
                    f"fail-closed: kill_switch active flag not bool "
                    f"(got {type(active_val).__name__})"
                )
            elif active_val:
                ks_active = True
                ks_reason = (
                    reason_val if reason_val is not None
                    else "kill_switch active (no reason provided)"
                )
            else:
                ks_active = False
                ks_reason = None

    except Exception as e:
        ks_active = True
        ks_reason = (
            f"fail-closed: kill_switch raised "
            f"{type(e).__name__}: {e}"
        )

    cfg["kill_switch_active"] = ks_active
    cfg["kill_switch_reason"] = ks_reason
    return cfg


def _find_new_candles(klines, last_processed_ts):
    if not klines:
        return []
    if last_processed_ts is None:
        return list(range(len(klines)))
    return [
        i for i, c in enumerate(klines)
        if int(c[0]) > last_processed_ts
    ]


def _handle_hash_mismatch(state_dir, error):
    print()
    print("=" * 70)
    print("❌ STATE HASH MISMATCH — PAPER RUN ABORTED")
    print("=" * 70)
    print(f"  state_dir : {state_dir}")
    print(f"  error     : {error}")
    print()

    try:
        diagnostic = load_state(state_dir, strict=False)
        position = diagnostic.get("position")
    except Exception as diag_e:
        position = None
        print(f"  (could not read state for diagnostics: {diag_e})")
        print()

    if position is not None:
        print("  ⚠️  OPEN POSITION DETECTED")
        print(f"     side           : {position.get('side')}")
        print(f"     entry_price    : {position.get('entry_price')}")
        print(f"     remaining_size : {position.get('remaining_size')}")
        print()
        print("  Manual intervention required.")
    else:
        print("  No open position in state.")
        print("  Manual options:")
        print("    1. Restore the previous strategy, OR")
        print("    2. Perform an explicit migration.")

    print("  ⛔ state.json and events.jsonl have NOT been modified.")
    print("=" * 70)


def _write_paper_status_atomic(state_dir, status):
    os.makedirs(state_dir, exist_ok=True)
    target = os.path.join(state_dir, "paper_status.json")
    fd, tmp_path = tempfile.mkstemp(
        dir=state_dir, prefix=".paper_status_", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                status, f,
                indent=2, sort_keys=True,
                ensure_ascii=True, allow_nan=False,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def main() -> int:
    state_dir = os.environ.get("STATE_DIR", "./paper_state_dir")
    symbol = os.environ.get("SYMBOL", "BTC-SWAP-USDT")
    interval = os.environ.get("INTERVAL", "1h")
    start_iso = datetime.now(timezone.utc).isoformat()

    exit_code = 0
    run_status = "failed"
    error_message = None
    reconciler_status = None
    reconciler_summary = None
    last_processed = None
    candles_processed = 0
    catchup_lag = 0
    total_new = 0
    equity = None
    peak_equity = None
    has_position = None

    try:
        print("=" * 70)
        print("📄 PAPER TRADING RUN")
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
                _handle_hash_mismatch(state_dir, e)
                raise _RunComplete(1, "aborted", str(e))
        else:
            state = init_state(state_dir, initial_equity=1000.0,
                               symbol=symbol)
            print("✅ State initialized")

        last_processed = state.get("last_processed_candle_ts")
        equity = state["equity"]
        peak_equity = state["peak_equity"]
        has_position = state["position"] is not None

        print(f"  last_processed_candle_ts : {last_processed}")
        print(f"  equity                   : {equity}")
        print(f"  peak_equity              : {peak_equity}")
        print(f"  has_position             : {has_position}")
        print()

        print("📋 Fetching symbol specs...")
        try:
            specs = get_symbol_specs(symbol)
        except Exception as e:
            print(f"❌ symbol_specs failed: {e}")
            raise _RunComplete(1, "failed", f"symbol_specs: {e}")
        print(f"  contract_multiplier : {specs['contract_multiplier']}")
        print()

        print(f"📥 Fetching closed {interval} candles...")
        try:
            klines = fetch_closed_klines(symbol, interval, limit=300)
        except Exception as e:
            print(f"❌ data_feed failed: {e}")
            raise _RunComplete(1, "failed", f"data_feed: {e}")

        print(f"  received {len(klines)} closed candles")
        if len(klines) > 0:
            print(f"  last candle ts : {int(klines[-1][0])}")
            print(f"  last close     : {float(klines[-1][4])}")
        print()

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

        first_ts = int(klines[to_process[0]][0])
        last_to_process_ts = int(klines[to_process[-1]][0])

        print(f"🆕 {total_new} new candles detected")
        print(f"   processing    : {len(to_process)}")
        print(f"   catchup_lag   : {catchup_lag}")
        print()

        print("🧠 Computing indicators...")
        try:
            indicators = _build_indicators(klines)
        except Exception as e:
            print(f"❌ indicators failed: {e}")
            raise _RunComplete(1, "failed", f"indicators: {e}")
        print("  ✅ computed")
        print()

        print("🔍 Reconciler check...")
        now_ms = int(time.time() * 1000)
        try:
            rec = reconcile_with_data(state, klines, interval, now_ms)
            reconciler_status = rec["status"]
            reconciler_summary = rec["summary"]
            print(f"  status  : {reconciler_status}")
            print(f"  summary : {reconciler_summary}")
            if reconciler_status == "ERROR":
                print("❌ reconciler reported ERROR — aborting")
                raise _RunComplete(
                    1, "aborted",
                    f"reconciler: {reconciler_summary}",
                )
        except _RunComplete:
            raise
        except Exception as e:
            print(f"❌ reconciler raised: {type(e).__name__}: {e}")
            reconciler_status = "ERROR"
            reconciler_summary = f"raised: {type(e).__name__}: {e}"
            raise _RunComplete(1, "failed", f"reconciler: {e}")
        print()

        # B1: fail-loud اگر get_fee_config خطا بدهد
        try:
            engine_config = _build_engine_config_with_kill_switch(
                specs, state, state_dir, repo_root=os.getcwd(),
            )
        except Exception as e:
            print(f"❌ engine_config failed (fee config?): {e}")
            raise _RunComplete(1, "failed", f"engine_config: {e}")

        ks_active = engine_config.get("kill_switch_active")
        ks_reason = engine_config.get("kill_switch_reason")
        print(f"🛑 Kill switch:")
        print(f"   active : {ks_active}")
        print(f"   reason : {ks_reason}")
        print(f"   fees   : open={engine_config.get('open_taker_fee')}, "
              f"close={engine_config.get('close_taker_fee')}")
        print()

        print(f"⚙️  Processing {len(to_process)} candle(s)...")
        total_events_generated = 0

        for n, idx in enumerate(to_process, start=1):
            candle_ts = int(klines[idx][0])
            sig = evaluate_signal(indicators, idx)
            signal_label = sig.get("signal", "NO_TRADE")

            try:
                state, events = process_candle(
                    state, klines, indicators, idx, specs, engine_config,
                    signal=sig,
                )
            except Exception as e:
                print(f"  [{n}/{len(to_process)}] → ERROR: "
                      f"{type(e).__name__}: {e}")
                raise _RunComplete(
                    1, "failed",
                    f"paper_engine at idx={idx}: {type(e).__name__}: {e}",
                )

            if events:
                n_app, n_sk = append_events(state_dir, events)
                total_events_generated += n_app

            state["last_processed_candle_ts"] = candle_ts
            state["symbol"] = symbol
            state["contract_multiplier"] = specs["contract_multiplier"]
            state["last_close_price"] = float(klines[idx][4])
            state["catchup_lag_candles"] = catchup_lag

            try:
                save_state(state_dir, state)
            except Exception as e:
                raise _RunComplete(
                    1, "failed",
                    f"save_state at idx={idx}: {type(e).__name__}: {e}",
                )

            candles_processed += 1
            print(f"  [{n}/{len(to_process)}] idx={idx} ts={candle_ts} "
                  f"signal={signal_label} events={len(events)}")

        print()

        equity = state["equity"]
        peak_equity = state["peak_equity"]
        has_position = state["position"] is not None
        last_processed = state.get("last_processed_candle_ts")

        total_events_count = events_count(state_dir)

        print("=" * 70)
        print("📌 PAPER RUN SUMMARY")
        print("=" * 70)
        print(f"  candles_processed  : {candles_processed}")
        print(f"  catchup_lag        : {catchup_lag}")
        print(f"  events_generated   : {total_events_generated}")
        print(f"  total_events       : {total_events_count}")
        print(f"  equity             : {equity}")
        print(f"  peak_equity        : {peak_equity}")
        print(f"  has_position       : {has_position}")
        print(f"  daily_loss         : {state['daily_loss']}")
        print(f"  weekly_loss        : {state['weekly_loss']}")
        print(f"  circuit_breaker    : {state['circuit_breaker_active']}")
        print("=" * 70)
        print("✅ PAPER RUN COMPLETED")

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
            print(f"\n📝 paper_status.json written "
                  f"(run_status={run_status}, exit_code={exit_code})")
        except Exception as we:
            print(f"\n❌ failed to write paper_status.json: "
                  f"{type(we).__name__}: {we}")
            if exit_code == 0:
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())