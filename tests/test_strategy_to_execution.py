"""
test_strategy_to_execution.py — Integration test بین Strategy و Execution.

مسیر واقعی (بدون mock در سیگنال):

    klines → indicators → strategy → signal
                                    ↓
                       signal_to_order → OrderRequest
                                    ↓
                       order_executor → Decision

قراردادهای صریح (Verified):
  - compute_all(klines, ema_fast_p, ema_slow_p, donchian_p, atr_p)
  - evaluate_signal(indicators, index)
  - order_executor.evaluate(req, specs, state, config, ref_price)
    که config.get("leverage_cap") و config.get("max_concurrent_positions") را می‌خواند.

تست‌های کلیدی (نه قابل قبول با NO_TRADE):
  - در فاز uptrend، حتماً حداقل یک LONG در کل فاز تولید شود
  - در فاز downtrend، حتماً حداقل یک SHORT تولید شود
  - در فاز flat، هرگز LONG یا SHORT تولید نشود

پوشش: 9 تست.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from indicators import compute_all
from strategy import evaluate_signal
from signal_to_order import signal_to_order
from order_executor import evaluate


WARMUP = 220
SYMBOL = "BTC-SWAP-USDT"

TEST_CONFIG = {
    "leverage_cap": 3,
    "max_concurrent_positions": 1,
}

SPECS = {
    "contract_multiplier": 0.001,
    "min_qty": 0.0001,
    "max_qty": 120.0,
    "qty_step": 0.0001,
    "min_notional": 0.0,
    "tick_size": 0.1,
    "max_leverage": 200.0,
}

STATE_EMPTY = {
    "symbol": SYMBOL,
    "position": None,
    "pending_entry": None,
    "circuit_breaker_active": False,
    "kill_switch": False,
}


# ============================================================
# Fixtures
# ============================================================

def _make_klines(n_warmup, n_phase, drift, vol, start=100.0):
    """
    n_warmup کندل flat، سپس n_phase کندل با drift.
    LCG noise → deterministic.
    """
    klines = []
    price = start
    total = n_warmup + n_phase
    for i in range(total):
        if i < n_warmup:
            d, v = 0.0, 0.02
        else:
            d, v = drift, vol
        noise = ((i * 9301 + 49297) % 233280) / 233280.0 - 0.5
        noise *= v
        o = price
        c = o + d + noise
        h = max(o, c) + 0.02
        l = min(o, c) - 0.02
        klines.append([i * 3_600_000,
                       str(o), str(h), str(l), str(c), "1000"])
        price = c
    return klines


def _flat(n=400, price=100.0):
    return [
        [i * 3_600_000,
         str(price), str(price + 0.5), str(price - 0.5),
         str(price), "1000"]
        for i in range(n)
    ]


def _all_signals(klines):
    """indicators یک بار، سپس evaluate_signal روی هر index."""
    ind = compute_all(klines, ema_fast_p=50, ema_slow_p=200,
                      donchian_p=20, atr_p=14)
    return [evaluate_signal(ind, i) for i in range(len(klines))]


# ============================================================
# Tests
# ============================================================

def test_uptrend_produces_at_least_one_long():
    """
    در فاز uptrend بعد از warmup، حتماً حداقل یک LONG باید تولید شود.
    اگر نشد، داده مصنوعی ضعیف است یا منطق strategy اشتباه.
    """
    klines = _make_klines(n_warmup=WARMUP, n_phase=180,
                          drift=0.3, vol=0.05, start=100.0)
    signals = _all_signals(klines)

    longs = [
        i for i, s in enumerate(signals)
        if i >= WARMUP and s["signal"] == "LONG"
    ]
    assert len(longs) >= 1, (
        f"در uptrend (drift=0.3, {len(klines)} کندل) هیچ LONG تولید نشد. "
        f"signals بعد از warmup: "
        f"{[signals[i]['signal'] for i in range(WARMUP, len(signals))][:20]}"
    )
    print(f"✅ test_uptrend_produces_at_least_one_long "
          f"({len(longs)} LONG candles)")


def test_downtrend_produces_at_least_one_short():
    klines = _make_klines(n_warmup=WARMUP, n_phase=180,
                          drift=-0.3, vol=0.05, start=500.0)
    signals = _all_signals(klines)

    shorts = [
        i for i, s in enumerate(signals)
        if i >= WARMUP and s["signal"] == "SHORT"
    ]
    assert len(shorts) >= 1, (
        f"در downtrend هیچ SHORT تولید نشد. "
        f"signals بعد از warmup: "
        f"{[signals[i]['signal'] for i in range(WARMUP, len(signals))][:20]}"
    )
    print(f"✅ test_downtrend_produces_at_least_one_short "
          f"({len(shorts)} SHORT candles)")


def test_flat_never_produces_long_or_short():
    klines = _flat(400)
    signals = _all_signals(klines)
    for i, s in enumerate(signals):
        assert s["signal"] == "NO_TRADE", (
            f"flat data produced {s['signal']} at index {i}"
        )
    print("✅ test_flat_never_produces_long_or_short")


def test_real_long_signal_becomes_accepted_order():
    """
    اولین LONG در فاز uptrend → OrderRequest → Decision accepted.
    این تست مسیر کامل را با یک سیگنال واقعی اثبات می‌کند.
    """
    klines = _make_klines(n_warmup=WARMUP, n_phase=180,
                          drift=0.3, vol=0.05, start=100.0)
    signals = _all_signals(klines)

    long_idx = next(
        (i for i in range(WARMUP, len(signals))
         if signals[i]["signal"] == "LONG"),
        None,
    )
    assert long_idx is not None, "هیچ LONG تولید نشد — تست بی‌معنی است"

    sig = dict(signals[long_idx])
    sig["symbol"] = SYMBOL
    req = signal_to_order(sig, sizing={"qty": 0.001, "leverage": 3})
    assert req is not None
    assert req.side == "LONG"
    assert req.intent == "ENTRY"

    ref_price = float(klines[long_idx][4])
    d = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                 ref_price=ref_price)
    assert d.accepted is True, (
        f"LONG signal at idx {long_idx} rejected — gate={d.gate} "
        f"reason={d.reason}"
    )
    assert d.order is not None
    assert d.order["side"] == "LONG"
    assert d.order["intent"] == "ENTRY"
    print(f"✅ test_real_long_signal_becomes_accepted_order "
          f"(idx={long_idx})")


def test_real_short_signal_becomes_accepted_order():
    klines = _make_klines(n_warmup=WARMUP, n_phase=180,
                          drift=-0.3, vol=0.05, start=500.0)
    signals = _all_signals(klines)

    short_idx = next(
        (i for i in range(WARMUP, len(signals))
         if signals[i]["signal"] == "SHORT"),
        None,
    )
    assert short_idx is not None, "هیچ SHORT تولید نشد"

    sig = dict(signals[short_idx])
    sig["symbol"] = SYMBOL
    req = signal_to_order(sig, sizing={"qty": 0.001, "leverage": 3})
    assert req.side == "SHORT"

    ref_price = float(klines[short_idx][4])
    d = evaluate(req, SPECS, STATE_EMPTY, TEST_CONFIG,
                 ref_price=ref_price)
    assert d.accepted is True, (
        f"SHORT signal at idx {short_idx} rejected — "
        f"gate={d.gate} reason={d.reason}"
    )
    print(f"✅ test_real_short_signal_becomes_accepted_order "
          f"(idx={short_idx})")


def test_no_trade_never_becomes_order():
    klines = _flat(400)
    signals = _all_signals(klines)
    for sig in signals:
        full = dict(sig)
        full["symbol"] = SYMBOL
        req = signal_to_order(full)
        assert req is None
    print("✅ test_no_trade_never_becomes_order")


def test_warmup_no_signal():
    """قبل از warmup → همیشه NO_TRADE."""
    klines = _make_klines(n_warmup=WARMUP, n_phase=100,
                          drift=0.3, vol=0.05)
    signals = _all_signals(klines)
    for i in range(min(WARMUP, len(signals))):
        assert signals[i]["signal"] == "NO_TRADE", (
            f"سیگنال در warmup idx={i}: {signals[i]['signal']}"
        )
    print("✅ test_warmup_no_signal")


def test_signal_determinism():
    klines = _make_klines(n_warmup=WARMUP, n_phase=180,
                          drift=0.3, vol=0.05)
    s1 = _all_signals(klines)
    s2 = _all_signals(klines)
    assert [s["signal"] for s in s1] == [s["signal"] for s in s2]
    print("✅ test_signal_determinism")


def test_full_pipeline_summary():
    """
    خلاصه‌ی وضعیت هر ۳ رژیم — برای دیباگ.
    ادعا: flat → فقط NO_TRADE، uptrend → حداقل یک LONG،
         downtrend → حداقل یک SHORT.
    """
    up = _make_klines(WARMUP, 180, 0.3, 0.05, 100.0)
    down = _make_klines(WARMUP, 180, -0.3, 0.05, 500.0)
    flat = _flat(400)

    for label, kl in (("up", up), ("down", down), ("flat", flat)):
        sigs = _all_signals(kl)
        post = [s["signal"] for s in sigs[WARMUP:]]
        n_long = post.count("LONG")
        n_short = post.count("SHORT")
        n_none = post.count("NO_TRADE")
        print(f"  [{label}] LONG={n_long} SHORT={n_short} NO_TRADE={n_none}")

    up_post = [s["signal"] for s in _all_signals(up)[WARMUP:]]
    down_post = [s["signal"] for s in _all_signals(down)[WARMUP:]]
    flat_post = [s["signal"] for s in _all_signals(flat)[WARMUP:]]

    assert "LONG" in up_post
    assert "SHORT" in down_post
    assert all(s == "NO_TRADE" for s in flat_post)
    print("✅ test_full_pipeline_summary")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_uptrend_produces_at_least_one_long,
        test_downtrend_produces_at_least_one_short,
        test_flat_never_produces_long_or_short,
        test_real_long_signal_becomes_accepted_order,
        test_real_short_signal_becomes_accepted_order,
        test_no_trade_never_becomes_order,
        test_warmup_no_signal,
        test_signal_determinism,
        test_full_pipeline_summary,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Strategy → Execution Integration Tests")
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