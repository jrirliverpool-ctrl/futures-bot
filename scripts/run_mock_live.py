import json
import os
import sys
import tempfile
import time
import logging
from datetime import datetime, timezone

from paper_state_manager import (
    init_state,
    load_state,
    save_state,
    state_exists,
    append_events,
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

from execution_manager import ExecutionManager
from order_executor import OrderRequest
from telegram_notifier import notify_run_status


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_mock_live")

MAX_CATCHUP_CANDLES = 100
PAPER_STATUS_SCHEMA = "1.0.0"
MIN_REQUIRED_CANDLES = 250

# This runner is deliberately simulation-only.
EXECUTION_MODE = "MOCK_LIVE"


class _RunComplete(Exception):
    def __init__(self, exit_code, run_status, error_message=None):
        super().__init__(f"{run_status}:{exit_code}")
        self.exit_code = exit_code
        self.run_status = run_status
        self.error_message = error_message


def _build_indicators(klines):
    params = get_indicator_params()

    return compute_all(
        klines,
        ema_fast_p=params["ema_fast_p"],
        ema_slow_p=params["ema_slow_p"],
        donchian_p=params["donchian_p"],
        atr_p=params["atr_p"],
    )


def _enrich_specs_for_engine(specs):
    cfg = get_engine_config()
    cfg["contract_multiplier_for_pnl"] = specs["contract_multiplier"]

    fee_cfg = get_fee_config()
    cfg["open_taker_fee"] = fee_cfg["open_taker_fee"]
    cfg["close_taker_fee"] = fee_cfg["close_taker_fee"]

    return cfg


def _build_engine_config_with_kill_switch(
    specs,
    state,
    state_dir,
    repo_root=None,
):
    cfg = _enrich_specs_for_engine(specs)

    # Fail-closed: if Kill Switch verification fails,
    # new entries must remain blocked.
    ks_active = True
    ks_reason = "fail-closed: kill_switch check did not complete"

    try:
        result = is_kill_switch_active(
            state_dir,
            state=state,
            repo_root=repo_root,
        )

        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], bool)
        ):
            ks_active = True
            ks_reason = "fail-closed: invalid kill_switch output"

        else:
            active, reason = result
            ks_active = active

            if active:
                ks_reason = reason or "kill_switch active"
            else:
                ks_reason = None

    except Exception as exc:
        ks_active = True
        ks_reason = (
            f"fail-closed: kill_switch raised "
            f"{type(exc).__name__}: {exc}"
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
        i
        for i, candle in enumerate(klines)
        if int(candle[0]) > last_processed_ts
    ]


def _write_paper_status_atomic(state_dir, status):
    os.makedirs(state_dir, exist_ok=True)

    target = os.path.join(
        state_dir,
        "paper_status.json",
    )

    fd, tmp_path = tempfile.mkstemp(
        dir=state_dir,
        prefix=".paper_status_",
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                status,
                file,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )

            file.flush()
            os.fsync(file.fileno())

        os.replace(tmp_path, target)

    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        raise


def _create_order_from_event(ev, symbol, leverage=1):
    intent = ev.get("intent")

    order_intent = (
        "EXIT"
        if intent == "TP1_HIT"
        else intent
    )

    qty = ev.get("qty")
    price = ev.get("price", 0.0)

    request = OrderRequest(
        symbol=symbol,
        side=ev["side"],
        order_type="MARKET",
        qty=qty,
        price=None,
        leverage=leverage,
        intent=order_intent,
    )

    return request, price, intent


def _refresh_status_from_state(state):
    """Read reporting values from the latest in-memory state."""

    if not isinstance(state, dict):
        return None, None, None, None

    return (
        state.get("last_processed_candle_ts"),
        state.get("equity"),
        state.get("peak_equity"),
        state.get("position") is not None,
    )


def main() -> int:
    state_dir = os.environ.get(
        "STATE_DIR",
        "./paper_state_dir",
    )

    symbol = os.environ.get(
        "SYMBOL",
        "BTC-SWAP-USDT",
    )

    interval = os.environ.get(
        "INTERVAL",
        "1h",
    )

    start_iso = datetime.now(timezone.utc).isoformat()

    exit_code = 0
    run_status = "failed"
    error_message = None

    candles_processed = 0
    total_new = 0
    catchup_lag = 0

    equity = None
    peak_equity = None
    has_position = None
    last_processed = None

    reconciler_status = None
    reconciler_summary = None

    strategy_hash = None
    state = None
    specs = None
    engine_config = None
    em = None

    # Strict interpretation:
    # only the exact value "1" enables this runner.
    safe_mode = (
        os.environ.get("APEX_SAFE_MODE") == "1"
    )

    def execution_callback(pending_info, state_ref):
        if (
            em is None
            or specs is None
            or engine_config is None
        ):
            logger.error(
                "Execution callback called before initialization."
            )
            return False

        request, ref_price, _original_intent = (
            _create_order_from_event(
                pending_info,
                symbol,
                engine_config.get("leverage", 1),
            )
        )

        if request.intent == "ENTRY":
            pending_entry = state_ref.get("pending_entry")

            if pending_entry:
                if (
                    pending_entry.get("signal_candle_ts")
                    == pending_info.get("signal_candle_ts")
                ):
                    state_ref["pending_entry"] = None
                else:
                    return False

        # Persist state before execution-manager action.
        decision = em.execute(
            request,
            specs,
            state_ref,
            engine_config,
            ref_price=ref_price,
            save_state_fn=lambda current_state: save_state(
                state_dir,
                current_state,
            ),
        )

        return decision.accepted

    try:
        print("=" * 70)
        print("MOCK LIVE TRADING RUN")
        print("=" * 70)
        print(f"STATE_DIR: {state_dir}")
        print(f"SYMBOL: {symbol}")
        print(f"INTERVAL: {interval}")
        print(f"SAFE_MODE: {safe_mode}")
        print(f"EXECUTION_MODE: {EXECUTION_MODE}")
        print("=" * 70)

        # P0 guard:
        # Do not load, initialize, or mutate trading state unless
        # the explicit safety flag is enabled.
        if not safe_mode:
            raise _RunComplete(
                1,
                "aborted",
                (
                    "APEX_SAFE_MODE must be exactly '1'. "
                    "Runner stopped before state access."
                ),
            )

        # Defense in depth: this runner must never select LIVE.
        if EXECUTION_MODE != "MOCK_LIVE":
            raise _RunComplete(
                1,
                "aborted",
                "Execution mode safety invariant violated.",
            )

        try:
            strategy_hash = compute_strategy_hash()

            print(f"Strategy hash: {strategy_hash}")

        except Exception as exc:
            raise _RunComplete(
                1,
                "failed",
                (
                    f"strategy_hash: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        was_existing = state_exists(state_dir)

        if was_existing:
            try:
                state = load_state(state_dir)

            except ValueError as exc:
                print(
                    f"State validation failed: {exc}"
                )

                raise _RunComplete(
                    1,
                    "aborted",
                    f"state_validation: {exc}",
                )

            print("State loaded (existing)")

            if state.get("pending_events"):
                pending_events = state["pending_events"]

                print(
                    "Recovering "
                    f"{len(pending_events)} pending events."
                )

                append_events(
                    state_dir,
                    pending_events,
                    strategy_hash=strategy_hash,
                )

                state["pending_events"] = []
                save_state(state_dir, state)

            if state.get("halt_trading"):
                print(
                    "SYSTEM HALTED: "
                    "No new entries will be permitted."
                )

        else:
            state = init_state(
                state_dir,
                initial_equity=1000.0,
                symbol=symbol,
            )

            print("State initialized")

            state["pending_events"] = []
            state["halt_trading"] = False

            save_state(state_dir, state)

        (
            last_processed,
            equity,
            peak_equity,
            has_position,
        ) = _refresh_status_from_state(state)

        print(f"Last processed candle: {last_processed}")
        print(f"Equity: {equity}")
        print(f"Peak equity: {peak_equity}")
        print(f"Position open: {has_position}")

        print("Fetching symbol specifications...")

        specs = get_symbol_specs(symbol)

        print(
            "Contract multiplier: "
            f"{specs['contract_multiplier']}"
        )

        print(
            f"Fetching closed {interval} candles..."
        )

        klines = fetch_closed_klines(
            symbol,
            interval,
            limit=300,
        )

        candle_count = (
            len(klines)
            if klines is not None
            else 0
        )

        print(
            f"Received closed candles: {candle_count}"
        )

        if candle_count < MIN_REQUIRED_CANDLES:
            raise _RunComplete(
                0,
                "insufficient_data",
                (
                    f"received {candle_count} candles; "
                    f"minimum required is {MIN_REQUIRED_CANDLES}"
                ),
            )

        print("Computing indicators...")
        indicators = _build_indicators(klines)

        print("Reconciler check...")

        now_ms = int(time.time() * 1000)

        rec = reconcile_with_data(
            state,
            klines,
            interval,
            now_ms,
        )

        reconciler_status = rec.get("status")
        reconciler_summary = rec.get("summary")

        print(
            f"Reconciler status: {reconciler_status}"
        )

        print(
            f"Reconciler summary: {reconciler_summary}"
        )

        if reconciler_status == "ERROR":
            raise _RunComplete(
                1,
                "aborted",
                f"reconciler: {reconciler_summary}",
            )

        if last_processed is None:
            if has_position or state.get("pending_entry"):
                raise _RunComplete(
                    1,
                    "aborted",
                    "bootstrap_conflict",
                )

            latest_ts = int(klines[-1][0])

            print(
                "BOOTSTRAP: "
                f"Setting last_processed to {latest_ts}"
            )

            state["last_processed_candle_ts"] = latest_ts
            state["symbol"] = symbol
            state["contract_multiplier"] = (
                specs["contract_multiplier"]
            )
            state["last_close_price"] = float(
                klines[-1][4]
            )

            save_state(state_dir, state)

            (
                last_processed,
                equity,
                peak_equity,
                has_position,
            ) = _refresh_status_from_state(state)

            raise _RunComplete(
                0,
                "bootstrap",
            )

        new_indices = _find_new_candles(
            klines,
            last_processed,
        )

        if not new_indices:
            print(
                "No new closed candle. "
                f"Last processed: {last_processed}"
            )

            raise _RunComplete(
                0,
                "no_op",
            )

        total_new = len(new_indices)

        to_process = new_indices[:MAX_CATCHUP_CANDLES]

        catchup_lag = (
            total_new - len(to_process)
        )

        print(f"New candles detected: {total_new}")
        print(f"Candles to process: {len(to_process)}")
        print(f"Catch-up lag: {catchup_lag}")

        engine_config = _build_engine_config_with_kill_switch(
            specs,
            state,
            state_dir,
            repo_root=os.getcwd(),
        )

        # Do not make this mode configurable through environment
        # variables. The runner is simulation-only.
        em = ExecutionManager(
            mode=EXECUTION_MODE,
            client=None,
        )

        print(
            "Kill switch active: "
            f"{engine_config.get('kill_switch_active')}"
        )

        for n, idx in enumerate(to_process, start=1):
            candle_ts = int(klines[idx][0])

            if state.get("halt_trading"):
                signal = None
            else:
                signal = evaluate_signal(
                    indicators,
                    idx,
                )

            state, events = process_candle(
                state,
                klines,
                indicators,
                idx,
                specs,
                engine_config,
                signal=signal,
                execution_callback=execution_callback,
                exit_callback=execution_callback,
            )

            # Persist latest candle and reporting values together
            # with the existing pending-event outbox.
            state["last_processed_candle_ts"] = candle_ts
            state["symbol"] = symbol
            state["contract_multiplier"] = (
                specs["contract_multiplier"]
            )
            state["last_close_price"] = float(
                klines[idx][4]
            )
            state["catchup_lag_candles"] = catchup_lag

            # OUTBOX:
            # 1. Persist pending events in state.
            # 2. Append events to the event log.
            # 3. Clear pending events and persist again.
            state["pending_events"] = events

            save_state(
                state_dir,
                state,
            )

            if events:
                append_events(
                    state_dir,
                    events,
                    strategy_hash=strategy_hash,
                )

            state["pending_events"] = []

            save_state(
                state_dir,
                state,
            )

            candles_processed += 1

            (
                last_processed,
                equity,
                peak_equity,
                has_position,
            ) = _refresh_status_from_state(state)

            print(
                f"[{n}/{len(to_process)}] "
                f"ts={candle_ts} "
                f"events={len(events)} "
                f"equity={equity}"
            )

        if catchup_lag > 0:
            run_status = "partial_catchup"
        else:
            run_status = "success"

        exit_code = 0

    except _RunComplete as complete:
        exit_code = complete.exit_code
        run_status = complete.run_status
        error_message = complete.error_message

    except Exception as exc:
        run_status = "failed"
        exit_code = 1

        error_message = (
            f"{type(exc).__name__}: {exc}"
        )

        logger.exception(
            "MOCK_LIVE run failed."
        )

    finally:
        if isinstance(state, dict):
            (
                last_processed,
                equity,
                peak_equity,
                has_position,
            ) = _refresh_status_from_state(state)

        finished_iso = datetime.now(
            timezone.utc
        ).isoformat()

        status = {
            "schema_version": PAPER_STATUS_SCHEMA,
            "run_started_at_iso": start_iso,
            "run_finished_at_iso": finished_iso,
            "run_status": run_status,
            "run_exit_code": exit_code,
            "strategy_hash": strategy_hash,
            "state_dir": state_dir,
            "symbol": symbol,
            "interval": interval,
            "execution_mode": EXECUTION_MODE,
            "safe_mode_enabled": safe_mode,
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

        # Status persistence failure is itself a failed run.
        # The notification must report the corrected result.
        try:
            _write_paper_status_atomic(
                state_dir,
                status,
            )

            print(
                "paper_status.json written "
                f"(run_status={run_status}, "
                f"exit_code={exit_code})"
            )

        except Exception as write_exc:
            logger.exception(
                "Could not write paper_status.json."
            )

            write_error = (
                "paper_status_write: "
                f"{type(write_exc).__name__}: {write_exc}"
            )

            if error_message:
                error_message = (
                    f"{error_message}; {write_error}"
                )
            else:
                error_message = write_error

            run_status = "failed"
            exit_code = 1

            status["run_status"] = "failed"
            status["run_exit_code"] = 1
            status["error_message"] = error_message

        try:
            notify_run_status(status)

        except Exception as notify_exc:
            # Telegram must never alter trading state or run result.
            logger.warning(
                "Telegram notifier raised: %s",
                type(notify_exc).__name__,
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())