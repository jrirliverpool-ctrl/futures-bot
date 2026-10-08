"""
test_hash_regression.py — Regression test for strategy_hash.

⚠️  این تست، محافظ است، نه زندان.

اگر این تست Fail شد، یعنی پارامترهای استراتژی تغییر کرده‌اند.

فرآیند مجاز تغییر استراتژی
══════════════════════════════════════════════════════════════

  تغییر strategy_config.py
         ↓
  strategy_hash عوض می‌شود
         ↓
  این تست FAIL می‌شود
         ↓
  آیا تغییر عمدی است؟
    │
    ├── خیر → پارامتر را به مقدار قبلی برگردان
    │
    └── بله → ادامه بده:
          1. docs/strategy_v1.md را به‌روزرسانی کن
          2. docs/CHANGELOG.md را با ورودی جدید + hash جدید اضافه کن
          3. GOLDEN_HASH را در همین فایل با hash جدید جایگزین کن
          4. تغییرات را Commit کن
          5. CI باید دوباره سبز شود

توجه: در این نسخه، GOLDEN_HASH با PLACEHOLDER مقداردهی شده
تا پس از اجرای اول، hash واقعی از لاگ CI استخراج و جایگزین شود.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from compute_hash import compute_strategy_hash, get_strategy_metadata
from strategy_config import STRATEGY_CONFIG, get_strategy_config


# ═══════════════════════════════════════════════════════════
# GOLDEN VALUES — به‌روزرسانی در Batch 2
# ═══════════════════════════════════════════════════════════
GOLDEN_HASH = "sha256:PLACEHOLDER_WILL_BE_FILLED"
GOLDEN_VERSION = "1.0.1"
# ═══════════════════════════════════════════════════════════


def test_golden_hash_matches():
    current = compute_strategy_hash()
    if current != GOLDEN_HASH:
        msg = (
            "\n"
            "╔══════════════════════════════════════════════════════╗\n"
            "║  ⚠️  STRATEGY HASH CHANGED                          ║\n"
            "╚══════════════════════════════════════════════════════╝\n"
            f"  Expected : {GOLDEN_HASH}\n"
            f"  Got      : {current}\n"
            "\n"
            "  اگر عمدی است: GOLDEN_HASH را در همین فایل آپدیت کن.\n"
            "  اگر تصادفی است: پارامتر را برگردان.\n"
            "╚══════════════════════════════════════════════════════╝\n"
        )
        raise AssertionError(msg)
    print(f"✅ test_golden_hash_matches passed")
    print(f"   hash = {current}")


def test_golden_version_matches():
    meta = get_strategy_metadata()
    assert meta["version"] == GOLDEN_VERSION, (
        f"Version mismatch: expected {GOLDEN_VERSION}, got {meta['version']}"
    )
    print(f"✅ test_golden_version_matches passed (v{meta['version']})")


def test_full_metadata():
    meta = get_strategy_metadata()
    assert meta["version"] == GOLDEN_VERSION
    assert meta["hash"] == GOLDEN_HASH
    print(f"✅ test_full_metadata passed")


def test_hash_derives_from_strategy_config():
    h1 = compute_strategy_hash()
    h2 = compute_strategy_hash(get_strategy_config())

    manual = {
        "version": "1.0.1",
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
        "risk_per_trade": 0.01,
        "max_leverage": 3,
        "leverage_per_trade": 3,
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.07,
        "total_dd_limit": 0.15,
        "cooldown_candles": 2,
        "slippage_pct": 0.0005,
    }
    h3 = compute_strategy_hash(manual)

    assert h1 == h2, "default ≠ explicit strategy_config"
    assert h2 == h3, "strategy_config ≠ manual reconstruction"
    print("✅ test_hash_derives_from_strategy_config passed")


def test_config_keys_match_manual_set():
    manual_keys = {
        "version", "timeframe",
        "ema_fast_period", "ema_slow_period",
        "donchian_period", "atr_period",
        "sl_atr_mult", "tp1_atr_mult",
        "be_trigger_at_r", "be_sl_offset_atr",
        "trailing_atr_mult", "time_exit_candles",
        "risk_per_trade", "max_leverage", "leverage_per_trade",
        "daily_loss_limit", "weekly_loss_limit", "total_dd_limit",
        "cooldown_candles", "slippage_pct",
    }
    actual_keys = set(STRATEGY_CONFIG.keys())

    assert actual_keys == manual_keys, (
        f"Config keys mismatch.\n"
        f"  only in config: {sorted(actual_keys - manual_keys)}\n"
        f"  only in manual: {sorted(manual_keys - actual_keys)}"
    )
    print("✅ test_config_keys_match_manual_set passed")


if __name__ == "__main__":
    tests = [
        test_golden_hash_matches,
        test_golden_version_matches,
        test_full_metadata,
        test_hash_derives_from_strategy_config,
        test_config_keys_match_manual_set,
    ]
    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Hash Regression Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)