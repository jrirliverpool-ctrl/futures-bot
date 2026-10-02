"""
strategy_config.py — Single Source of Truth برای پارامترهای ApexQuant V1.

تمام ماژول‌های زیر از این فایل پارامتر می‌گیرند:
  - compute_hash.py
  - indicators.py (در Step 9)
  - paper_engine.py (در Step 9)
  - backtest.py (Step 8d)
  - run_paper.py (Step 9)

تغییر هر پارامتر در این فایل، به‌طور خودکار:
  - strategy_hash را تغییر می‌دهد
  - در trade_log.csv منعکس می‌شود
  - Backtest/Paper آینده را با config جدید اجرا می‌کند

Spec: docs/strategy_v1.md
"""


# ============================================================
# Strategy Configuration (داخل Hash)
# ============================================================

STRATEGY_CONFIG = {
    "version": "1.0.0",

    # --- Timeframe ---
    "timeframe": "1h",

    # --- Indicator Parameters ---
    "ema_fast_period": 50,
    "ema_slow_period": 200,
    "donchian_period": 20,
    "atr_period": 14,

    # --- Trade Management ---
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 2.0,
    "be_trigger_at_r": 1.0,        # BE trigger at +1R
    "be_sl_offset_atr": 0.1,       # BE SL = Entry ± 0.1×ATR
    "trailing_atr_mult": 1.5,
    "time_exit_candles": 48,

    # --- Risk Management ---
    "risk_per_trade": 0.01,
    "max_leverage": 3,
    "daily_loss_limit": 0.03,
    "weekly_loss_limit": 0.07,
    "total_dd_limit": 0.15,

    # --- Execution ---
    "cooldown_candles": 2,
    "slippage_pct": 0.0005,
}


# ============================================================
# Runtime Defaults (خارج از Hash)
# ============================================================

DEFAULT_SYMBOL = "BTC-SWAP-USDT"

INTERVAL_MS_MAP = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


# ============================================================
# Canonical Keys (immutable snapshot)
# ============================================================

CANONICAL_KEYS = tuple(sorted(STRATEGY_CONFIG.keys()))


# ============================================================
# Accessors
# ============================================================

def get_strategy_config(overrides: dict = None) -> dict:
    """
    config استاندارد استراتژی + overrides اختیاری.

    overrides فقط برای تست سناریوهای جایگزین. در Production
    هیچ override ای اعمال نمی‌شود.

    Raises:
        ValueError: اگر overrides شامل کلید ناشناخته باشد.
    """
    config = dict(STRATEGY_CONFIG)
    if overrides:
        unknown = set(overrides.keys()) - set(STRATEGY_CONFIG.keys())
        if unknown:
            raise ValueError(
                f"Unknown config keys in overrides: {sorted(unknown)}"
            )
        config.update(overrides)
    return config


def get_indicator_params() -> dict:
    """
    پارامترهای اندیکاتور برای compute_all() در indicators.py.
    Returns dict با کلیدهای موردانتظار compute_all:
        ema_fast_p, ema_slow_p, donchian_p, atr_p
    """
    return {
        "ema_fast_p": STRATEGY_CONFIG["ema_fast_period"],
        "ema_slow_p": STRATEGY_CONFIG["ema_slow_period"],
        "donchian_p": STRATEGY_CONFIG["donchian_period"],
        "atr_p": STRATEGY_CONFIG["atr_period"],
    }


def get_engine_config() -> dict:
    """
    پارامترهای Paper Engine / Backtest Engine.
    این خروجی با فرمت DEFAULT_CONFIG در paper_engine.py سازگار است.
    """
    interval_label = STRATEGY_CONFIG["timeframe"]
    if interval_label not in INTERVAL_MS_MAP:
        raise ValueError(
            f"Unknown timeframe in strategy config: {interval_label}"
        )
    return {
        "risk_per_trade": STRATEGY_CONFIG["risk_per_trade"],
        "sl_atr_mult": STRATEGY_CONFIG["sl_atr_mult"],
        "tp1_atr_mult": STRATEGY_CONFIG["tp1_atr_mult"],
        "trailing_atr_mult": STRATEGY_CONFIG["trailing_atr_mult"],
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
    """interval_ms برای timeframe فعال."""
    tf = STRATEGY_CONFIG["timeframe"]
    if tf not in INTERVAL_MS_MAP:
        raise ValueError(f"Unknown timeframe: {tf}")
    return INTERVAL_MS_MAP[tf]