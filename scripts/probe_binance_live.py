"""
probe_binance_live.py — تأیید اتصال زنده به Binance Public API.

هدف (محدود و صریح):
  - اثبات اینکه GitHub Actions می‌تواند به endpoint عمومی Binance وصل شود
  - تأیید ساختار پاسخ واقعی (نه Mock)
  - تأیید ترتیب زمانی صعودی و یکتایی timestamps
  - تأیید نبود گپ در داده‌های دریافتی

آنچه این Probe اثبات نمی‌کند:
  - دسترسی به داده‌های چندساله تاریخی (فقط داده‌ی اخیر را می‌سنجد)
  - صحت صفحه‌بندی بازه‌های طولانی (نیاز به Probe جداگانه)
  - فیلتر کندل‌های بسته (مسئولیت Backtest)
  - کیفیت داده برای استراتژی (فقط ساختار بررسی می‌شود)

هیچ Backtest اجرا نمی‌شود.
هیچ state ذخیره نمی‌شود.
هیچ سفارشی ارسال نمی‌شود.

فرض: این اسکریپت در همان پوشه‌ی binance_feed.py قرار دارد
      (هر دو در scripts/). پس Import مستقیم کار می‌کند.
"""

import time

from binance_feed import (
    fetch_binance_klines,
    summarize_candles,
    InvalidCandleError,
)


SYMBOL = "BTCUSDT"
INTERVAL = "1h"


# ============================================================
# Helpers
# ============================================================

def _check_timestamps_sorted_unique(candles: list) -> tuple:
    """
    بررسی صریح:
      - timestamps یکتا هستند
      - ترتیب صعودی است

    Returns: (ok: bool, detail: str)
    """
    ts_list = [int(c[0]) for c in candles]

    if len(ts_list) != len(set(ts_list)):
        seen = set()
        dups = []
        for t in ts_list:
            if t in seen:
                dups.append(t)
            seen.add(t)
        return False, f"duplicate timestamps: {dups[:3]}"

    if ts_list != sorted(ts_list):
        for i in range(1, len(ts_list)):
            if ts_list[i] <= ts_list[i - 1]:
                return False, (
                    f"not ascending at index {i}: "
                    f"{ts_list[i-1]} -> {ts_list[i]}"
                )
        return False, "not sorted"

    return True, "sorted & unique"


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 72)
    print("🔍 PROBE BINANCE LIVE — Connectivity Verification")
    print("=" * 72)
    print(f"  symbol   : {SYMBOL}")
    print(f"  interval : {INTERVAL}")
    print(f"  purpose  : تأیید اتصال زنده (نه تأیید داده‌ی تاریخی)")
    print("=" * 72)

    # ---- Stage 1: 10 candles ----
    print("\n[Stage 1] Fetching 10 candles (small test)...")
    try:
        small = fetch_binance_klines(
            SYMBOL, INTERVAL, limit=10,
            max_retries=3, backoff_base=0.5, timeout=15,
        )
    except InvalidCandleError as e:
        print(f"❌ InvalidCandleError: {e}")
        return 1
    except Exception as e:
        print(f"❌ Fetch failed: {type(e).__name__}: {e}")
        return 1

    print(f"  received {len(small)} candles")
    if len(small) != 10:
        print(f"❌ Expected 10 candles, got {len(small)}")
        return 1
    print("  ✅ 10 candles received")

    # ---- Stage 2: Structural checks ----
    print("\n[Stage 2] Structural checks on 10-candle batch...")
    s = summarize_candles(small, INTERVAL)
    print(f"  count       : {s['count']}")
    print(f"  first_iso   : {s['first_iso']}")
    print(f"  last_iso    : {s['last_iso']}")
    print(f"  duplicates  : {s['duplicates']}")
    print(f"  gap_count   : {s['gap_count']}")
    print(f"  total_miss  : {s['total_missing']}")

    if s["duplicates"] > 0:
        print(f"❌ Duplicates found: {s['duplicates']}")
        return 1
    if s["gap_count"] > 0:
        print(f"❌ Gaps found: {s['gap_count']}")
        return 1

    # ---- Stage 2b: Explicit sorted/unique check ----
    ok, detail = _check_timestamps_sorted_unique(small)
    if not ok:
        print(f"❌ Timestamp check failed: {detail}")
        return 1
    print(f"  ✅ timestamps: {detail}")

    # ---- Stage 3: 100 candles ----
    print("\n[Stage 3] Fetching 100 candles (bigger test)...")
    try:
        big = fetch_binance_klines(
            SYMBOL, INTERVAL, limit=100,
            max_retries=3, backoff_base=0.5, timeout=15,
        )
    except Exception as e:
        print(f"❌ Fetch failed: {type(e).__name__}: {e}")
        return 1

    print(f"  received {len(big)} candles")
    if len(big) != 100:
        print(f"❌ Expected 100 candles, got {len(big)}")
        return 1

    s2 = summarize_candles(big, INTERVAL)
    print(f"  first_iso   : {s2['first_iso']}")
    print(f"  last_iso    : {s2['last_iso']}")
    print(f"  gap_count   : {s2['gap_count']}")
    print(f"  duplicates  : {s2['duplicates']}")

    if s2["gap_count"] > 0:
        print(f"❌ Gaps found in 100-batch: {s2['gap_count']}")
        return 1
    if s2["duplicates"] > 0:
        print(f"❌ Duplicates in 100-batch: {s2['duplicates']}")
        return 1

    ok2, detail2 = _check_timestamps_sorted_unique(big)
    if not ok2:
        print(f"❌ Timestamp check failed (100): {detail2}")
        return 1
    print(f"  ✅ timestamps: {detail2}")

    # ---- Stage 4: Price sample ----
    print("\n[Stage 4] Last 3 candle closes:")
    for c in big[-3:]:
        ts = int(c[0])
        close = float(c[4])
        iso = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts / 1000))
        print(f"  {iso}: {close}")

    # ---- Final ----
    print()
    print("=" * 72)
    print("✅ BINANCE LIVE CONNECTIVITY VERIFIED")
    print("=" * 72)
    print(f"  symbol          : {SYMBOL}")
    print(f"  interval        : {INTERVAL}")
    print(f"  10-batch        : OK")
    print(f"  100-batch       : OK")
    print(f"  structure       : valid")
    print(f"  timestamps      : sorted & unique")
    print(f"  gaps            : none in received batch")
    print()
    print("  ⚠️ این Probe فقط اتصال زنده را تأیید کرد.")
    print("  ⚠️ دریافت تاریخی چندساله نیازمند Probe جداگانه است.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())