"""
test_nan_validation.py — تست اعتبارسنجی NaN/Infinity در دو لایه.

هدف: اثبات اینکه داده‌ی نامعتبر باید در دو نقطه رد شود:
  - لایه ۱: data_feed (پیش از بازگشت به caller)
  - لایه ۲: indicators.compute_all (مستقل از منبع)

⚠️ پیش‌بینی: اکثر تست‌های این فایل در نسخه‌ی فعلی کد شکست می‌خورند.
   شکست، یافته‌ی واقعی است — نه خطای تست. هدف این است که این
   شکست‌ها مستند شوند، سپس اصلاحات اعمال شود، سپس تست‌ها سبز شوند.

⚠️ هیچ فایل تولیدی در این مرحله تغییر نمی‌کند.
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from data_feed import fetch_klines, fetch_closed_klines
from indicators import compute_all
from strategy import evaluate_signal


# ============================================================
# Constants
# ============================================================

BASE_TS = 1_698_796_800_000   # 2023-11-01T00:00:00Z (aligned 1h)
HOUR = 3_600_000


# ============================================================
# Mocks (بدون شبکه)
# ============================================================

class MockResponse:
    def __init__(self, status, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data if json_data is not None else []
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class MockSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._responses:
            raise RuntimeError("MockSession exhausted")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ============================================================
# Candle builders
# ============================================================

def _candle_ok(ts):
    return [ts, "100.0", "101.0", "99.0", "100.5", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_nan_open(ts):
    return [ts, "nan", "101.0", "99.0", "100.5", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_inf_high(ts):
    return [ts, "100.0", "inf", "99.0", "100.5", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_neg_inf_low(ts):
    return [ts, "100.0", "101.0", "-inf", "100.5", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_zero_close(ts):
    return [ts, "100.0", "101.0", "99.0", "0", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_negative_close(ts):
    return [ts, "100.0", "101.0", "99.0", "-50", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_low_above_high(ts):
    return [ts, "100.0", "99.0", "101.0", "100.5", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _candle_short(ts):
    return [ts, "100.0", "101.0"]


def _candle_nonnumeric(ts):
    return [ts, "abc", "101.0", "99.0", "100.5", "1000.0",
            ts + HOUR - 1, "0", 0, "0", "0", "0"]


def _batch_ok(start_ts, n):
    return [_candle_ok(start_ts + i * HOUR) for i in range(n)]


def _resp_ok(candles):
    return MockResponse(200, json_data=candles)


# ============================================================
# Layer 1 — data_feed must reject invalid candles
# ============================================================

def test_fetch_klines_rejects_nan_open():
    session = MockSession([_resp_ok([_candle_nan_open(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return  # انتظار: رد شد
    assert False, f"fetch_klines accepted NaN candle: {result}"


def test_fetch_klines_rejects_positive_inf():
    session = MockSession([_resp_ok([_candle_inf_high(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    assert False, f"fetch_klines accepted +Inf candle: {result}"


def test_fetch_klines_rejects_negative_inf():
    session = MockSession([_resp_ok([_candle_neg_inf_low(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    assert False, f"fetch_klines accepted -Inf candle: {result}"


def test_fetch_klines_rejects_zero_price():
    session = MockSession([_resp_ok([_candle_zero_close(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    assert False, f"fetch_klines accepted zero-close candle: {result}"


def test_fetch_klines_rejects_negative_price():
    session = MockSession([_resp_ok([_candle_negative_close(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    assert False, f"fetch_klines accepted negative-price candle: {result}"


def test_fetch_klines_rejects_low_above_high():
    session = MockSession([_resp_ok([_candle_low_above_high(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    assert False, f"fetch_klines accepted low>high candle: {result}"


def test_fetch_klines_rejects_short_candle():
    session = MockSession([_resp_ok([_candle_short(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError, IndexError):
        return
    assert False, f"fetch_klines accepted too-short candle: {result}"


def test_fetch_klines_rejects_nonnumeric_ohlc():
    session = MockSession([_resp_ok([_candle_nonnumeric(BASE_TS)])])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    assert False, f"fetch_klines accepted non-numeric OHLC: {result}"


def test_fetch_klines_rejects_non_monotonic_timestamps():
    bad = [
        _candle_ok(BASE_TS + HOUR),
        _candle_ok(BASE_TS),          # نزولی
        _candle_ok(BASE_TS + 2 * HOUR),
    ]
    session = MockSession([_resp_ok(bad)])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    ts_list = [int(c[0]) for c in result]
    assert False, f"fetch_klines accepted non-monotonic timestamps: {ts_list}"


def test_fetch_klines_rejects_duplicate_timestamps():
    bad = [
        _candle_ok(BASE_TS),
        _candle_ok(BASE_TS),          # تکرار
        _candle_ok(BASE_TS + HOUR),
    ]
    session = MockSession([_resp_ok(bad)])
    try:
        result = fetch_klines(
            "BTC-SWAP-USDT", "1h", session=session, backoff_base=0,
        )
    except (ValueError, TypeError):
        return
    ts_list = [int(c[0]) for c in result]
    assert False, f"fetch_klines accepted duplicate timestamps: {ts_list}"


# ============================================================
# Layer 2 — compute_all must reject invalid inputs
# ============================================================

def test_compute_all_rejects_nan_candle():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_nan_open(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted NaN candle"


def test_compute_all_rejects_positive_inf():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_inf_high(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted +Inf candle"


def test_compute_all_rejects_negative_inf():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_neg_inf_low(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted -Inf candle"


def test_compute_all_rejects_zero_price():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_zero_close(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted zero close price"


def test_compute_all_rejects_negative_price():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_negative_close(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted negative close price"


def test_compute_all_rejects_low_above_high():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_low_above_high(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted low>high candle"


def test_compute_all_rejects_nonnumeric():
    bad = _batch_ok(BASE_TS, 5)
    bad[2] = _candle_nonnumeric(BASE_TS + 2 * HOUR)
    try:
        compute_all(bad)
    except (ValueError, TypeError):
        return
    assert False, "compute_all accepted non-numeric OHLC"


# ============================================================
# Integration — strategy with NaN indicators
# ============================================================

def test_strategy_returns_no_trade_on_nan_indicators():
    """
    اگر اندیکاتورها NaN باشند، strategy باید NO_TRADE برگرداند.
    (این تست فعلاً pass می‌شود چون NaN در مقایسه False می‌دهد،
    ولی برای تأیید رفتار امنیتی مفید است.)
    """
    n = 300
    nan_vals = [None] * 250 + [float("nan")] * 50
    indicators = {
        "timestamps": [BASE_TS + i * HOUR for i in range(n)],
        "closes": [100.0] * n,
        "ema_fast": list(nan_vals),
        "ema_slow": list(nan_vals),
        "donchian_high": list(nan_vals),
        "donchian_low": list(nan_vals),
        "atr": list(nan_vals),
    }
    sig = evaluate_signal(indicators, 299)
    assert sig["signal"] == "NO_TRADE", (
        f"strategy returned {sig['signal']} on NaN indicators"
    )


# ============================================================
# Integration — run_paper with NaN data
# ============================================================

def test_run_paper_state_not_advanced_on_invalid_data():
    """
    با NaN candles در ورودی، state نباید جلو برود.

    ⚠️ نکته‌ی مهم: در نسخه‌ی فعلی کد، این تست ممکن است به‌طور
    اتفاقی PASS شود — چون save_state با json.dump allow_nan=False
    جلوی نوشتن state را می‌گیرد، ولی این محافظت تصادفی است،
    نه validation. پس از افزودن validation، این تست باید برای
    دلیل درست pass شود (رد صریح پیش از پردازش).

    تست یک ویژگی امنیتی است: last_processed_candle_ts نباید
    روی داده‌ی نامعتبر جلو برود.
    """
    import run_paper as rp

    with tempfile.TemporaryDirectory() as td:
        state_dir = os.path.join(td, "state")
        os.makedirs(state_dir, exist_ok=True)

        nan_candles = [
            _candle_nan_open(BASE_TS + i * HOUR) for i in range(300)
        ]

        fake_specs = {
            "symbol": "BTC-SWAP-USDT",
            "contract_multiplier": 0.001,
            "min_qty": 1.0,
            "max_qty": 1_000_000.0,
            "qty_step": 1.0,
            "min_notional": 5.0,
            "max_leverage": 100.0,
        }

        buf = io.StringIO()
        exit_code = None

        with patch.dict(os.environ, {
            "STATE_DIR": state_dir,
            "SYMBOL": "BTC-SWAP-USDT",
            "INTERVAL": "1h",
        }), \
             patch.object(rp, "get_symbol_specs",
                          return_value=fake_specs), \
             patch.object(rp, "fetch_closed_klines",
                          return_value=nan_candles), \
             patch.object(rp, "is_kill_switch_active",
                          return_value=(False, None)), \
             redirect_stdout(buf):
            try:
                rp.main()
            except SystemExit as e:
                exit_code = e.code

        state_path = os.path.join(state_dir, "state.json")
        if not os.path.exists(state_path):
            return  # state ساخته نشد؛ محافظت برقرار است

        with open(state_path) as f:
            saved = json.load(f)

        last_ts = saved.get("last_processed_candle_ts")
        assert last_ts is None, (
            f"state advanced to {last_ts} despite NaN input "
            f"(exit_code={exit_code})"
        )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Layer 1 — data_feed (10)
        test_fetch_klines_rejects_nan_open,
        test_fetch_klines_rejects_positive_inf,
        test_fetch_klines_rejects_negative_inf,
        test_fetch_klines_rejects_zero_price,
        test_fetch_klines_rejects_negative_price,
        test_fetch_klines_rejects_low_above_high,
        test_fetch_klines_rejects_short_candle,
        test_fetch_klines_rejects_nonnumeric_ohlc,
        test_fetch_klines_rejects_non_monotonic_timestamps,
        test_fetch_klines_rejects_duplicate_timestamps,
        # Layer 2 — compute_all (7)
        test_compute_all_rejects_nan_candle,
        test_compute_all_rejects_positive_inf,
        test_compute_all_rejects_negative_inf,
        test_compute_all_rejects_zero_price,
        test_compute_all_rejects_negative_price,
        test_compute_all_rejects_low_above_high,
        test_compute_all_rejects_nonnumeric,
        # Integration (2)
        test_strategy_returns_no_trade_on_nan_indicators,
        test_run_paper_state_not_advanced_on_invalid_data,
    ]

    passed = 0
    failed = 0
    failures = []

    print("=" * 68)
    print(f"🧪 NaN/Infinity Validation Tests ({len(tests)} total)")
    print("=" * 68)
    print()

    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"❌ {t.__name__}: {exc}")
            failures.append((t.__name__, str(exc)))
            failed += 1
        except Exception as exc:
            print(f"❌ {t.__name__}: {type(exc).__name__}: {exc}")
            failures.append((t.__name__, f"{type(exc).__name__}: {exc}"))
            failed += 1

    print()
    print("=" * 68)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 68)

    if failures:
        print()
        print("⚠️  تحلیل شکست‌ها (یافته‌های واقعی برای اصلاح):")
        print()
        for name, msg in failures:
            print(f"  • {name}")
            print(f"    {msg}")
            print()

    if failed:
        sys.exit(1)