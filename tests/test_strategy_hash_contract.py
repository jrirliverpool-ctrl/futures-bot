"""
test_strategy_hash_contract.py — تست قرارداد hash و اتصال config به engine.

سه قرارداد مستقل:
  ۱. قرارداد hash: پایداری، فرمت، حساسیت، جداسازی
  ۲. قرارداد canonical JSON: بدون whitespace، کلیدها مرتب، NaN/Inf رد
  ۳. قرارداد اتصال config→engine: پارامتر مؤثر، واقعاً رفتار موتور را تغییر دهد

⚠️ این فایل هیچ فایلی را تغییر نمی‌دهد، هیچ شبکه‌ای مصرف نمی‌کند.
"""

import json
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from compute_hash import (
    compute_strategy_hash,
    config_to_canonical_json,
    get_strategy_metadata,
    verify_hash,
)
from strategy_config import (
    STRATEGY_CONFIG,
    CANONICAL_KEYS,
    get_strategy_config,
    get_engine_config,
    get_indicator_params,
)


# ============================================================
# 1. Hash — Format and Determinism (4 tests)
# ============================================================

def test_hash_format():
    h = compute_strategy_hash()
    assert h.startswith("sha256:"), f"got {h[:20]!r}"
    digest = h[len("sha256:"):]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_deterministic_repeated():
    assert compute_strategy_hash() == compute_strategy_hash()


def test_hash_deterministic_explicit_vs_implicit():
    assert compute_strategy_hash() == compute_strategy_hash(
        get_strategy_config()
    )


def test_hash_deterministic_dict_order_invariant():
    c1 = dict(STRATEGY_CONFIG)
    c2 = dict(reversed(list(STRATEGY_CONFIG.items())))
    assert compute_strategy_hash(c1) == compute_strategy_hash(c2)


# ============================================================
# 2. Hash — Sensitivity (2 tests)
# ============================================================

def test_hash_sensitive_to_every_scalar_key():
    """
    تغییر هر کلید اسکالر باید hash را تغییر دهد.

    اگر کلیدی غیر اسکالر پیدا شود، تست صریحاً شکست می‌خورد تا
    آن کلید به یک تست اختصاصی نیاز پیدا کند (نه رد بی‌صدا).
    """
    base = dict(STRATEGY_CONFIG)
    h_base = compute_strategy_hash(base)

    non_scalar = []
    not_sensitive = []

    for key, original in STRATEGY_CONFIG.items():
        if isinstance(original, bool):
            modified_val = not original
        elif isinstance(original, int):
            modified_val = original + 1
        elif isinstance(original, float):
            modified_val = original * 1.5 + 0.001
        elif isinstance(original, str):
            modified_val = original + "_x"
        else:
            non_scalar.append((key, type(original).__name__))
            continue

        modified = dict(base)
        modified[key] = modified_val
        if compute_strategy_hash(modified) == h_base:
            not_sensitive.append(key)

    assert not non_scalar, (
        f"non-scalar config keys need dedicated tests: {non_scalar}"
    )
    assert not not_sensitive, (
        f"hash not sensitive to keys: {not_sensitive}"
    )


def test_hash_version_sensitivity():
    base = dict(STRATEGY_CONFIG)
    modified = dict(base)
    modified["version"] = "9.99.99-test"
    assert compute_strategy_hash(modified) != compute_strategy_hash(base)


# ============================================================
# 3. Hash — Isolation from Runtime (2 tests)
# ============================================================

def test_hash_rejects_unknown_override():
    """
    get_strategy_config(overrides) باید کلیدهای ناشناخته را رد کند.

    این تست قرارداد فعلی فایل strategy_config را تأیید می‌کند؛
    اگر API تابع عوض شود، تست نیاز به بازبینی دارد.
    """
    runtime_keys = [
        "symbol", "equity", "initial_equity", "timestamp",
        "state", "kill_switch_active", "last_processed_candle_ts",
    ]
    for key in runtime_keys:
        try:
            get_strategy_config({key: "x"})
            assert False, f"runtime key {key!r} accepted"
        except ValueError:
            pass


def test_config_does_not_contain_runtime_fields():
    forbidden = (
        "symbol", "equity", "initial_equity", "peak_equity",
        "state", "position", "pending_entry",
        "kill_switch", "kill_switch_active", "kill_switch_reason",
        "timestamp", "last_processed_candle_ts",
        "cooldown_until_index",
    )
    for key in forbidden:
        assert key not in STRATEGY_CONFIG, (
            f"runtime field {key!r} present in STRATEGY_CONFIG"
        )


# ============================================================
# 4. Canonical JSON (2 tests)
# ============================================================

def test_canonical_json_key_order_uses_json_parser():
    """
    ترتیب کلیدها باید الفبایی باشد.

    از json.loads با object_pairs_hook استفاده می‌شود تا اگر مقدار
    رشته‌ای حاوی کاما یا کاراکتر خاص باشد، پارس درست انجام شود.
    """
    cj = config_to_canonical_json(dict(STRATEGY_CONFIG))

    assert " " not in cj, "canonical JSON must be compact (no spaces)"

    # object_pairs_hook=dict preserves insertion order در Python 3.7+
    parsed = json.loads(cj)
    # برای ترتیب دقیق، از object_pairs_hook=list استفاده می‌کنیم
    pairs = json.loads(cj, object_pairs_hook=lambda p: p)
    keys_in_order = [k for k, _ in pairs]
    assert keys_in_order == sorted(keys_in_order), (
        f"keys not sorted: {keys_in_order}"
    )
    # تطبیق کلیدها با config اصلی
    assert set(parsed.keys()) == set(STRATEGY_CONFIG.keys())


def test_canonical_json_rejects_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        modified = dict(STRATEGY_CONFIG)
        modified["risk_per_trade"] = bad
        try:
            config_to_canonical_json(modified)
            assert False, f"accepted {bad!r}"
        except (ValueError, TypeError):
            pass


# ============================================================
# 5. verify_hash (4 tests)
# ============================================================

def test_verify_hash_valid():
    config = get_strategy_config()
    assert verify_hash(config, compute_strategy_hash(config)) is True


def test_verify_hash_mismatch():
    config = get_strategy_config()
    h = compute_strategy_hash(config)
    wrong = h[:-1] + ("0" if h[-1] != "0" else "1")
    assert verify_hash(config, wrong) is False


def test_verify_hash_non_string():
    config = get_strategy_config()
    for bad in (None, 123, b"sha256:abc", []):
        assert verify_hash(config, bad) is False


def test_verify_hash_case_sensitive_documented():
    """
    verify_hash فعلی case-sensitive است.
    hash با hex uppercase نباید پذیرفته شود.
    این تصمیم طراحی مستند می‌شود.
    """
    config = get_strategy_config()
    h = compute_strategy_hash(config)
    h_upper = "sha256:" + h[len("sha256:"):].upper()
    assert verify_hash(config, h_upper) is False


# ============================================================
# 6. Metadata (1 test)
# ============================================================

def test_metadata_shape():
    m = get_strategy_metadata()
    assert isinstance(m, dict)
    assert set(m.keys()) == {"version", "hash"}
    assert m["version"] == STRATEGY_CONFIG["version"]
    assert m["hash"] == compute_strategy_hash()


# ============================================================
# 7. CANONICAL_KEYS (1 test)
# ============================================================

def test_canonical_keys_matches_config():
    assert CANONICAL_KEYS == tuple(sorted(STRATEGY_CONFIG.keys()))


# ============================================================
# 8. Connection Contract — structural (2 tests)
# ============================================================

def test_connection_engine_derives_from_config():
    """
    هر کلید get_engine_config() باید یا مستقیماً از STRATEGY_CONFIG
    بیاید، یا از نظر معنایی قابل‌ردیابی باشد (مثل interval_ms که
    از timeframe مشتق می‌شود).
    """
    engine = get_engine_config()
    indicator = get_indicator_params()

    # کلیدهای engine که مستقیماً در config نیستند ولی مشتق می‌شوند
    derived = {
        "interval_ms": "timeframe",
    }

    # کلیدهای engine که نامشان با config متفاوت است
    name_map = {
        "leverage": "leverage_per_trade",
        "leverage_cap": "max_leverage",
    }

    for engine_key in engine.keys():
        if engine_key in derived:
            config_key = derived[engine_key]
        else:
            config_key = name_map.get(engine_key, engine_key)
        assert config_key in STRATEGY_CONFIG, (
            f"engine key {engine_key!r} derives from "
            f"{config_key!r} which is not in STRATEGY_CONFIG"
        )

    indicator_name_map = {
        "ema_fast_p": "ema_fast_period",
        "ema_slow_p": "ema_slow_period",
        "donchian_p": "donchian_period",
        "atr_p": "atr_period",
    }
    for ind_key in indicator.keys():
        config_key = indicator_name_map.get(ind_key, ind_key)
        assert config_key in STRATEGY_CONFIG, (
            f"indicator key {ind_key!r} derives from "
            f"{config_key!r} which is not in STRATEGY_CONFIG"
        )


def test_connection_no_orphan_config_keys():
    """
    هر کلید STRATEGY_CONFIG باید یا در engine، یا در indicator،
    یا در لیست استثناهای مستند ظاهر شود.

    لیست استثنا فقط کلیدهایی را می‌پذیرد که دلیل مستند دارند.
    """
    explicit_exceptions = {
        "version": "metadata only — consumed by state_manager as "
                   "state['strategy_version'] and event['strategy_version']",
        "timeframe": "transformed to interval_ms in get_engine_config()",
    }

    # ساخت مجموعه‌ی کلید config که در engine یا indicator مصرف می‌شوند
    engine_name_map = {
        "leverage": "leverage_per_trade",
        "leverage_cap": "max_leverage",
        "interval_ms": "timeframe",
    }
    indicator_name_map = {
        "ema_fast_p": "ema_fast_period",
        "ema_slow_p": "ema_slow_period",
        "donchian_p": "donchian_period",
        "atr_p": "atr_period",
    }

    consumed = set()
    for k in get_engine_config().keys():
        consumed.add(engine_name_map.get(k, k))
    for k in get_indicator_params().keys():
        consumed.add(indicator_name_map.get(k, k))

    orphans = set(STRATEGY_CONFIG.keys()) - consumed - set(explicit_exceptions)

    assert not orphans, (
        f"STRATEGY_CONFIG keys not consumed by engine or indicators "
        f"and not in explicit_exceptions: {sorted(orphans)}. "
        f"هر کلید orphan یعنی تغییر config، hash را عوض می‌کند "
        f"ولی رفتار موتور ثابت می‌ماند."
    )


# ============================================================
# 9. Connection Contract — behavioral (1 test)
# ============================================================

def test_be_trigger_at_r_actually_affects_engine_behavior():
    """
    ⚠️ این تست عمداً رفتاری است، نه ساختاری.

    هدف: اثبات اینکه مقدار config['be_trigger_at_r'] واقعاً روی
    زمان فعال شدن BE در paper_engine اثر می‌گذارد.

    روش: یک موقعیت با entry=100، SL=98 (risk=2) می‌سازیم.
    کندلی با high=102.5 (بین 1R=102 و 2R=104).
      - با be_trigger_at_r=1.0 → BE باید فعال شود
      - با be_trigger_at_r=2.0 → BE نباید فعال شود
    اگر خروجی در دو حالت یکی بود، پارامتر hard-coded است.

    اگر این تست شکست بخورد، یافته‌ی واقعی است:
    be_trigger_at_r در config هست، hash را تغییر می‌دهد، ولی
    paper_engine مقدار 1R را hard-code می‌کند.
    """
    from paper_engine import manage_position_on_candle

    def make_pos():
        return {
            "side": "LONG",
            "entry_price": 100.0,
            "entry_candle_ts": 1000,
            "size": 1.0,
            "remaining_size": 1.0,
            "tp1_size": 0.0,
            "realized_pnl": 0.0,
            "initial_sl": 98.0,
            "current_sl": 98.0,
            "initial_tp1": 104.0,
            "tp1_hit": False,
            "be_moved": False,
            "trailing_active": False,
            "highest_high_since_entry": 100.0,
            "lowest_low_since_entry": 100.0,
            "candles_since_entry": 0,
            "atr_at_entry": 1.0,
            "signal_candle_ts": 900,
        }

    # کندل با high=102.5 (بین 1R=102 و 2R=104)
    candle = [2000, 100.0, 102.5, 99.0, 102.0, 1000.0]

    base_config = {
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 2.0,
        "trailing_atr_mult": 1.5,
        "be_sl_offset_atr": 0.1,
        "time_exit_candles": 48,
        "contract_multiplier_for_pnl": 1.0,
    }

    def be_moved_events(config, be_trigger_at_r):
        cfg = dict(config)
        cfg["be_trigger_at_r"] = be_trigger_at_r
        _, events, _ = manage_position_on_candle(
            make_pos(), candle, atr_current=1.0, config=cfg,
        )
        return [e for e in events if e.get("type") == "BE_MOVED"]

    at_1r = be_moved_events(base_config, 1.0)
    at_2r = be_moved_events(base_config, 2.0)

    assert len(at_1r) == 1, (
        f"BE should fire at 1R (high=102.5 >= trigger=102) — "
        f"got {len(at_1r)} events"
    )
    assert len(at_2r) == 0, (
        f"BE should NOT fire at 2R (high=102.5 < trigger=104) — "
        f"but engine fired BE_MOVED regardless. "
        f"be_trigger_at_r is orphaned: config changes hash but "
        f"engine hard-codes 1R."
    )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Format and determinism (4)
        test_hash_format,
        test_hash_deterministic_repeated,
        test_hash_deterministic_explicit_vs_implicit,
        test_hash_deterministic_dict_order_invariant,
        # Sensitivity (2)
        test_hash_sensitive_to_every_scalar_key,
        test_hash_version_sensitivity,
        # Isolation (2)
        test_hash_rejects_unknown_override,
        test_config_does_not_contain_runtime_fields,
        # Canonical JSON (2)
        test_canonical_json_key_order_uses_json_parser,
        test_canonical_json_rejects_non_finite,
        # verify_hash (4)
        test_verify_hash_valid,
        test_verify_hash_mismatch,
        test_verify_hash_non_string,
        test_verify_hash_case_sensitive_documented,
        # Metadata (1)
        test_metadata_shape,
        # CANONICAL_KEYS (1)
        test_canonical_keys_matches_config,
        # Connection — structural (2)
        test_connection_engine_derives_from_config,
        test_connection_no_orphan_config_keys,
        # Connection — behavioral (1)
        test_be_trigger_at_r_actually_affects_engine_behavior,
    ]

    passed = 0
    failed = 0
    failures = []

    print("=" * 68)
    print(f"🧪 Strategy Hash Contract Tests ({len(tests)} total)")
    print("=" * 68)

    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"❌ {t.__name__}: {exc}")
            failures.append((t.__name__, str(exc)))
            failed += 1

    print("=" * 68)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 68)

    if failures:
        print()
        print("⚠️  تحلیل شکست‌ها (احتمالاً یافته‌ی واقعی):")
        print()
        for name, msg in failures:
            print(f"  • {name}")
            print(f"    {msg}")
            print()

    if failed:
        sys.exit(1)