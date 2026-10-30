STRATEGY_CONFIG = {
    "version": "1.1.0",
    "timeframe": "1h",
    "ema_fast_period": 50,
    "ema_slow_period": 200,
    "donchian_period": 20,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 2.0,
    "be_trigger_at_r": 1.0,
    "be_sl_offset_atr": 0.1,
    "trailing_atr_mult": 1.5,
    "time_exit_candles": 48,
    "risk_per_trade": 0.001,
    "max_leverage": 3,
    "leverage_per_trade": 3,
    "daily_loss_limit": 0.03,
    "weekly_loss_limit": 0.07,
    "total_dd_limit": 0.15,
    "cooldown_candles": 2,
    "slippage_pct": 0.0005,
}

DEFAULT_SYMBOL = "BTC-SWAP-USDT"

INTERVAL_MS_MAP = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

CANONICAL_KEYS = tuple(sorted(STRATEGY_CONFIG.keys()))

def get_strategy_config(overrides: dict = None) -> dict:
    config = dict(STRATEGY_CONFIG)
    if overrides:
        unknown = set(overrides.keys()) - set(STRATEGY_CONFIG.keys())
        if unknown:
            raise ValueError(f"Unknown config keys in overrides: {sorted(unknown)}")
        config.update(overrides)
    return config

def get_indicator_params() -> dict:
    return {"ema_fast_p": STRATEGY_CONFIG["ema_fast_period"], "ema_slow_p": STRATEGY_CONFIG["ema_slow_period"], "donchian_p": STRATEGY_CONFIG["donchian_period"], "atr_p": STRATEGY_CONFIG["atr_period"]}

def get_engine_config() -> dict:
    interval_label = STRATEGY_CONFIG["timeframe"]
    if interval_label not in INTERVAL_MS_MAP:
        raise ValueError(f"Unknown timeframe in strategy config: {interval_label}")
    return {
        "risk_per_trade": STRATEGY_CONFIG["risk_per_trade"],
        "leverage": STRATEGY_CONFIG["leverage_per_trade"],
        "leverage_cap": STRATEGY_CONFIG["max_leverage"],
        "sl_atr_mult": STRATEGY_CONFIG["sl_atr_mult"],
        "tp1_atr_mult": STRATEGY_CONFIG["tp1_atr_mult"],
        "trailing_atr_mult": STRATEGY_CONFIG["trailing_atr_mult"],
        "be_trigger_at_r": STRATEGY_CONFIG["be_trigger_at_r"],
        "be_sl_offset_atr": STRATEGY_CONFIG["be_sl_offset_atr"],
        "time_exit_candles": STRATEGY_CONFIG["time_exit_candles"],
        "cooldown_candles": STRATEGY_CONFIG["cooldown_candles"],
        "daily_loss_limit": STRATEGY_CONFIG["daily_loss_limit"],
        "weekly_loss_limit": STRATEGY_CONFIG["weekly_loss_limit"],
        "total_dd_limit": STRATEGY_CONFIG["total_dd_limit"],
        "slippage_pct": STRATEGY_CONFIG["slippage_pct"],
        "interval_ms": INTERVAL_MS_MAP[interval_label],
    }

def get_timeframe_interval_ms() -> int:
    tf = STRATEGY_CONFIG["timeframe"]
    if tf not in INTERVAL_MS_MAP: raise ValueError(f"Unknown timeframe: {tf}")
    return INTERVAL_MS_MAP[tf]

def get_fee_config() -> dict:
    return {"open_taker_fee": 0.0005, "close_taker_fee": 0.0005, "open_maker_fee": 0.0002, "close_maker_fee": 0.0002, "source": "toobit-probe-2026-10-18"}