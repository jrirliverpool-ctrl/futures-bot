"""
strategy_flexible.py — Experimental Strategy (E2).
Only tests the SHORT hypothesis in transition regimes.
Normalizes EMA slope by ATR. Strictly follows the V1 signal contract.
"""
import math
from strategy import evaluate_signal as base_evaluate_signal, STRATEGY_VERSION

SLOPE_ATR_THRESHOLD = -0.15 

def _is_finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

def evaluate_flexible_signal(indicators: dict, index: int, slope_threshold: float = SLOPE_ATR_THRESHOLD) -> dict:
    base_sig = base_evaluate_signal(indicators, index)
    
    if base_sig.get("signal") in ("LONG", "SHORT"):
        return base_sig
        
    required_keys = ["timestamps", "closes", "ema_fast", "ema_slow", "donchian_low", "atr"]
    if not all(k in indicators for k in required_keys):
        return base_sig
        
    closes = indicators["closes"]
    ema_fast = indicators["ema_fast"]
    ema_slow = indicators["ema_slow"]
    atr = indicators["atr"]
    donchian_low = indicators["donchian_low"]
    
    if index < 1 or index >= len(closes):
        return base_sig
        
    close = closes[index]
    fast = ema_fast[index]
    slow = ema_slow[index]
    atr_val = atr[index]
    dl_prev = donchian_low[index - 1]
    fast_prev = ema_fast[index - 1]
    
    # ✅ اعتبارسنجی مقادیر
    if not all(_is_finite(x) for x in [close, fast, slow, atr_val, dl_prev, fast_prev]):
        return base_sig
    if atr_val <= 0:
        return base_sig

    ema_slope_normalized = (fast - fast_prev) / atr_val
    
    is_transition = (close < slow) and (fast > slow)
    is_breakdown = (close < dl_prev)
    has_strong_slope = (ema_slope_normalized < slope_threshold)
    
    if is_transition and is_breakdown and has_strong_slope:
        return {
            "signal": "SHORT",
            "reason": f"E2_FLEXIBLE: transition=True, slope={ema_slope_normalized:.3f} < {slope_threshold}",
            "signal_candle_ts": indicators["timestamps"][index],
            "close": close,
            "ema_fast": fast,
            "ema_slow": slow,
            "donchian_ref": dl_prev,
            "atr_at_signal": atr_val,
            "entry_reference_candle_index": index + 1,
            "strategy_version": STRATEGY_VERSION + "-E2"
        }
        
    return base_sig