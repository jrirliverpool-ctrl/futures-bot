"""
ApexQuant v1 — Donchian breakout bot (paper trading only).
Fixes over previous version:
  • uses CLOSED candles only (drops the still-forming bar)
  • correct trailing order: check exit → then update trail for next bar
  • idempotent: skips if latest closed bar already processed
  • atomic state write
  • prints performance summary at end of each run
"""
import os, json, csv
from datetime import datetime, timezone
from pathlib import Path
import ccxt, pandas as pd, numpy as np, requests

CFG = {
    "exchange": "kraken",
    "market":   "spot",
    "symbols":  ["BTC/USD", "ETH/USD", "SOL/USD"],
    "tf":       "15m",

    "dc": 20, "ema_f": 20, "ema_s": 50, "atr_p": 14,
    "atr_min_pct": 0.30, "atr_max_pct": 5.00,

    "risk": 0.01,
    "sl_atr": 1.5,
    "tp_r": 2.0,
    "trail_act_r": 1.0,
    "trail_atr": 2.0,

    "fee": 0.0026,      # Kraken taker spot
    "slip": 0.0005,
    "max_pos": 3,
}

PAPER  = os.getenv("PAPER", "1") == "1"
STATE  = Path("state.json")
TRADES = Path("trades.csv")
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT  = os.getenv("TG_CHAT", "").strip()


def tg(msg: str):
    print(msg)
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.ok:
            print(f"tg api: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"tg err: {e}")


def load_state():
    if not STATE.exists():
        return {"positions": [], "equity": 1000.0, "last_bar_ts": None}
    try:
        s = json.loads(STATE.read_text())
        s.setdefault("positions", [])
        s.setdefault("equity", 1000.0)
        s.setdefault("last_bar_ts", None)
        return s
    except Exception as e:
        print(f"⚠️ state corrupted ({e}) — starting fresh")
        return {"positions": [], "equity": 1000.0, "last_bar_ts": None}


def save_state(s):
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2, default=str))
    tmp.replace(STATE)


def log_trade(t):
    is_new = not TRADES.exists()
    with TRADES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(t.keys()))
        if is_new:
            w.writeheader()
        w.writerow(t)


def make_exchange():
    klass = getattr(ccxt, CFG["exchange"])
    return klass({"enableRateLimit": True})


# ─────────────────────────────────────────────────────────────
def indicators(df):
    df = df.copy()
    df["ema_f"] = df.close.ewm(span=CFG["ema_f"], adjust=False).mean()
    df["ema_s"] = df.close.ewm(span=CFG["ema_s"], adjust=False).mean()
    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low  - df.close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"]     = tr.ewm(alpha=1 / CFG["atr_p"], adjust=False).mean()
    df["atr_pct"] = df.atr / df.close * 100
    df["dc_h"]    = df.high.rolling(CFG["dc"]).max().shift(1)
    df["dc_l"]    = df.low.rolling(CFG["dc"]).min().shift(1)
    return df


def fetch_closed(e, symbol):
    """Fetch OHLCV and DROP the last (still-forming) candle."""
    raw = e.fetch_ohlcv(symbol, CFG["tf"], limit=300)
    if not raw or len(raw) < CFG["dc"] + 20:
        raise ValueError(f"not enough candles: {len(raw) if raw else 0}")
    raw = raw[:-1]                              # ← kill repaint
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")
    return indicators(df)


def signal(row):
    if row.atr_pct < CFG["atr_min_pct"] or row.atr_pct > CFG["atr_max_pct"]:
        return 0
    if row.close > row.dc_h and row.ema_f > row.ema_s: return 1
    if row.close < row.dc_l and row.ema_f < row.ema_s: return -1
    return 0


def size_position(equity, entry, sl):
    r = abs(entry - sl)
    return (equity * CFG["risk"]) / r if r > 0 else 0.0


def print_summary():
    if not TRADES.exists(): return
    try:
        df = pd.read_csv(TRADES)
    except Exception:
        return
    if df.empty: return

    wins, losses = df[df.R > 0], df[df.R <= 0]
    print("\n📊 PERFORMANCE (all-time)")
    print(f"   trades   : {len(df)}")
    print(f"   win rate : {len(wins)/len(df)*100:.1f}%")
    print(f"   avg R    : {df.R.mean():+.3f}")
    print(f"   net PnL  : ${df.pnl.sum():+.2f}")
    if len(wins):   print(f"   avg win  : {wins.R.mean():+.2f}R")
    if len(losses): print(f"   avg loss : {losses.R.mean():+.2f}R")


# ─────────────────────────────────────────────────────────────
def main():
    st  = load_state()
    now = datetime.now(timezone.utc)

    e = make_exchange()
    data = {}
    for sym in CFG["symbols"]:
        try:
            data[sym] = fetch_closed(e, sym)
        except Exception as ex:
            tg(f"⚠️ {sym}: {type(ex).__name__} — {str(ex)[:120]}")

    if not data:
        tg("❌ no data — abort")
        return

    latest_ts = max(df.index[-1] for df in data.values())

    tg(f"🤖 ApexQuant · {now:%Y-%m-%d %H:%M UTC} · PAPER={PAPER}")
    tg(f"   latest closed bar: {latest_ts}")

    # idempotency guard
    if st.get("last_bar_ts") == str(latest_ts):
        tg("   bar already processed — skip")
        print_summary()
        return

    # ── EXIT PASS (uses pre-existing SL/TP) ─────────────────
    keep = []
    for p in st["positions"]:
        if p["symbol"] not in data:
            keep.append(p); continue
        df = data[p["symbol"]]
        if latest_ts not in df.index:
            keep.append(p); continue
        row = df.loc[latest_ts]

        hi, lo, entry = row.high, row.low, p["entry"]
        risk = abs(entry - p["initial_sl"])

        exit_p, why = None, None
        if p["side"] == "long":
            if lo <= p["sl"]:      exit_p, why = p["sl"], "SL"
            elif hi >= p["tp"]:    exit_p, why = p["tp"], "TP"
        else:
            if hi >= p["sl"]:      exit_p, why = p["sl"], "SL"
            elif lo <= p["tp"]:    exit_p, why = p["tp"], "TP"

        if exit_p is not None:
            sign  = 1 if p["side"] == "long" else -1
            gross = sign * (exit_p - entry) * p["qty"]
            fees  = (entry + exit_p) * p["qty"] * CFG["fee"]
            pnl   = gross - fees
            r_mul = pnl / (risk * p["qty"])
            st["equity"] += pnl
            log_trade({
                "time":      str(latest_ts),
                "symbol":    p["symbol"],
                "side":      p["side"],
                "entry":     round(entry, 6),
                "exit":      round(exit_p, 6),
                "qty":       round(p["qty"], 8),
                "pnl":       round(pnl, 4),
                "R":         round(r_mul, 3),
                "reason":    why,
                "mae":       round(p.get("mae", 0), 3),
                "mfe":       round(p.get("mfe", 0), 3),
                "bars_held": p.get("bars_held", 0) + 1,
            })
            tg(f"✅ *{why}* {p['symbol']} {p['side'].upper()}  "
               f"R={r_mul:+.2f}  PnL=${pnl:+.2f}  eq=${st['equity']:.2f}")
            continue

        # not exited → update MFE/MAE, then trailing for NEXT bar
        if p["side"] == "long":
            p["mfe"] = max(p.get("mfe", 0), (hi - entry) / risk)
            p["mae"] = min(p.get("mae", 0), (lo - entry) / risk)
        else:
            p["mfe"] = max(p.get("mfe", 0), (entry - lo) / risk)
            p["mae"] = min(p.get("mae", 0), (entry - hi) / risk)

        if not p.get("trail", False) and p["mfe"] >= CFG["trail_act_r"]:
            p["trail"] = True

        if p["trail"]:
            if p["side"] == "long":
                p["sl"] = max(p["sl"], hi - CFG["trail_atr"] * row.atr)
            else:
                p["sl"] = min(p["sl"], lo + CFG["trail_atr"] * row.atr)

        p["bars_held"] = p.get("bars_held", 0) + 1
        keep.append(p)

    st["positions"] = keep

    # ── ENTRY PASS ──────────────────────────────────────────
    held = {p["symbol"] for p in st["positions"]}
    for sym, df in data.items():
        if len(st["positions"]) >= CFG["max_pos"]: break
        if sym in held: continue
        if latest_ts not in df.index: continue

        row = df.loc[latest_ts]
        if np.isnan(row.atr) or row.atr <= 0: continue
        sig = signal(row)
        if sig == 0: continue

        side  = "long" if sig == 1 else "short"
        price = row.close * (1 + CFG["slip"] * (1 if sig == 1 else -1))
        if side == "long":
            sl = price - CFG["sl_atr"] * row.atr
            tp = price + CFG["tp_r"] * CFG["sl_atr"] * row.atr
        else:
            sl = price + CFG["sl_atr"] * row.atr
            tp = price - CFG["tp_r"] * CFG["sl_atr"] * row.atr
        qty = size_position(st["equity"], price, sl)
        if qty <= 0: continue

        st["positions"].append({
            "symbol": sym, "side": side,
            "entry": price, "initial_sl": sl, "sl": sl, "tp": tp,
            "qty": qty, "opened": str(latest_ts),
            "trail": False, "mfe": 0.0, "mae": 0.0, "bars_held": 0,
        })
        tg(f"🚀 *OPEN* {sym} {side.upper()}  entry={price:.4f}  "
           f"SL={sl:.4f}  TP={tp:.4f}  qty={qty:.6f}")

    st["last_bar_ts"] = str(latest_ts)
    save_state(st)

    print(f"\ndone · open={len(st['positions'])} · eq=${st['equity']:.2f}")
    print_summary()


if __name__ == "__main__":
    main()