import copy
import logging
from paper_engine import process_candle as _v1_process_candle

logger = logging.getLogger(__name__)

def process_symbol_candle(
    state_v2: dict, symbol: str, klines: list, indicators: dict, i: int,
    specs: dict, config: dict, signal=None, execution_callback=None,
    exit_callback=None, allowed_symbols=None,
) -> tuple:
    sym_state = state_v2["symbols"][symbol]
    state_v1 = {
        "initial_equity": state_v2["initial_equity"], "equity": state_v2["equity"],
        "peak_equity": state_v2["peak_equity"], "daily_loss": state_v2["daily_loss"],
        "weekly_loss": state_v2["weekly_loss"], "loss_day_key": state_v2["loss_day_key"],
        "loss_week_key": state_v2["loss_week_key"], "circuit_breaker_active": state_v2["circuit_breaker_active"],
        "position": copy.deepcopy(sym_state["position"]), "pending_entry": copy.deepcopy(sym_state["pending_entry"]),
        "cooldown_until_index": sym_state["cooldown_until_index"], "last_processed_candle_ts": sym_state["last_processed_candle_ts"],
        "last_funding_ts": sym_state["last_funding_ts"],
    }

    def combined_execution_callback(info, state_ref):
        if info.get("intent") == "ENTRY":
            if state_v2.get("circuit_breaker_active"): return False
            if state_v2["symbols"][symbol]["pending_entry"] is not None: return False
        if execution_callback:
            return execution_callback(info, state_ref)
        return True

    state_v1_after, v1_events = _v1_process_candle(
        state_v1, klines, indicators, i, specs, config,
        signal=signal, execution_callback=combined_execution_callback, exit_callback=exit_callback
    )

    state_v2["equity"] = state_v1_after["equity"]
    state_v2["peak_equity"] = state_v1_after["peak_equity"]
    state_v2["daily_loss"] = state_v1_after["daily_loss"]
    state_v2["weekly_loss"] = state_v1_after["weekly_loss"]
    state_v2["loss_day_key"] = state_v1_after["loss_day_key"]
    state_v2["loss_week_key"] = state_v1_after["loss_week_key"]
    state_v2["circuit_breaker_active"] = state_v1_after["circuit_breaker_active"]

    sym_state["position"] = state_v1_after["position"]
    sym_state["pending_entry"] = state_v1_after["pending_entry"]
    sym_state["cooldown_until_index"] = state_v1_after["cooldown_until_index"]
    sym_state["last_processed_candle_ts"] = state_v1_after["last_processed_candle_ts"]
    sym_state["last_funding_ts"] = state_v1_after["last_funding_ts"]

    return state_v2, v1_events