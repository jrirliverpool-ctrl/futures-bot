import os, json, csv
from datetime import datetime, timezone
from pathlib import Path
import ccxt, pandas as pd, numpy as np, requests

CFG = {
    "exchange": "bybit", "market": "swap",
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "tf": "15m",
    "dc": 20, "ema_f": 20, "ema_s": 50, "atr_p": 14,
    "risk": 0.01,
    "sl_atr": 1.5, "tp_r": 2.0,
    "trail_act_r": 1.0, "trail_atr": 2.0,
    "fee": 0.0004, "slip": 0.0005,
    "max_pos": 3,
}
PAPER = os.getenv("PAPER", "1") == "1"
STATE, TRADES = Path("state.json"), Path("trades.csv")
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN", ""), os.getenv("TG_CHAT", "")

def tg(msg):
    print(msg)
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
                      timeout=10)
    except Exception as e: print("tg err", e)

def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"positions": [], "equity": 1000.0}
def save(s): STATE.write_text(json.dumps(s, indent=2, default=str))
def log(t):
    new = not TRADES.exists()
    with TRADES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(t.keys()))
        if new: w.writeheader()
        w.writerow(t)

def exchange():
    k = getattr(ccxt, CFG["exchange"]); e = k({"enableRateLimit": True})
    if CFG["market"] == "future": e.options["defaultType"] = "future"
    return e

def indicators(df):
    df = df.copy()
    df["ema_f"] = df.close.ewm(span=CFG["ema_f"], adjust=False).mean()
    df["ema_s"] = df.close.ewm(span=CFG["ema_s"], adjust=False).mean()
    tr = pd.concat([df.high-df.low, (df.high-df.close.shift()).abs(),
                    (df.low-df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/CFG["atr_p"], adjust=False).mean()
    df["atr_pct"] = df.atr/df.close*100
    df["dc_h"] = df.high.rolling(CFG["dc"]).max().shift(1)
    df["dc_l"] = df.low.rolling(CFG["dc"]).min().shift(1)
    return df

def signal(row):
    if row.atr_pct < 0.3 or row.atr_pct > 5: return 0
    if row.close > row.dc_h and row.ema_f > row.ema_s: return 1
    if row.close < row.dc_l and row.ema_f < row.ema_s: return -1
    return 0

def size(eq, entry, sl):
    r = abs(entry - sl); return (eq * CFG["risk"]) / r if r > 0 else 0

def fetch(e, sym):
    raw = e.fetch_ohlcv(sym, CFG["tf"], limit=200)
    return indicators(pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"]))

def main():
    st = load(); e = exchange(); now = datetime.now(timezone.utc)
    data = {s: fetch(e, s) for s in CFG["symbols"]}

    # 1. manage open positions
    keep = []
    for p in st["positions"]:
        df = data[p["symbol"]]; row = df.iloc[-1]
        hi, lo, entry = row.high, row.low, p["entry"]
        risk = abs(entry - p["initial_sl"])

        if p["side"] == "long":
            p["mfe"] = max(p.get("mfe", 0), (hi-entry)/risk)
            p["mae"] = min(p.get("mae", 0), (lo-entry)/risk)
        else:
            p["mfe"] = max(p.get("mfe", 0), (entry-lo)/risk)
            p["mae"] = min(p.get("mae", 0), (entry-hi)/risk)

        if p["mfe"] >= CFG["trail_act_r"]: p["trail"] = True

        if p.get("trail"):
            if p["side"] == "long": p["sl"] = max(p["sl"], hi - CFG["trail_atr"]*row.atr)
            else:                   p["sl"] = min(p["sl"], lo + CFG["trail_atr"]*row.atr)

        exit_p, why = None, None
        if p["side"] == "long":
            if lo <= p["sl"]: exit_p, why = p["sl"], "SL"
            elif hi >= p["tp"]: exit_p, why = p["tp"], "TP"
        else:
            if hi >= p["sl"]: exit_p, why = p["sl"], "SL"
            elif lo <= p["tp"]: exit_p, why = p["tp"], "TP"

        if exit_p is None: keep.append(p); continue

        sign = 1 if p["side"] == "long" else -1
        gross = sign * (exit_p - entry) * p["qty"]
        fees = (entry + exit_p) * p["qty"] * CFG["fee"]
        pnl = gross - fees; r = pnl / (risk * p["qty"])
        st["equity"] += pnl
        t = {"time": now.isoformat(), "symbol": p["symbol"], "side": p["side"],
             "entry": entry, "exit": exit_p, "qty": p["qty"],
             "pnl": round(pnl,4), "R": round(r,3), "reason": why,
             "mae": round(p["mae"],3), "mfe": round(p["mfe"],3)}
        log(t)
        tg(f"✅ *{why}* {p['symbol']} {p['side'].upper()}  R={r:+.2f}  PnL=${pnl:+.2f}  eq=${st['equity']:.2f}")
    st["positions"] = keep

    # 2. new entries
    held = {p["symbol"] for p in st["positions"]}
    for sym in CFG["symbols"]:
        if len(st["positions"]) >= CFG["max_pos"] or sym in held: continue
        row = data[sym].iloc[-1]
        sig = signal(row)
        if sig == 0 or np.isnan(row.atr): continue

        side = "long" if sig == 1 else "short"
        price = row.close * (1 + CFG["slip"] * (1 if sig == 1 else -1))
        if side == "long":
            sl = price - CFG["sl_atr"]*row.atr
            tp = price + CFG["tp_r"]*CFG["sl_atr"]*row.atr
        else:
            sl = price + CFG["sl_atr"]*row.atr
            tp = price - CFG["tp_r"]*CFG["sl_atr"]*row.atr
        qty = size(st["equity"], price, sl)
        if qty <= 0: continue

        st["positions"].append({"symbol": sym, "side": side, "entry": price,
            "initial_sl": sl, "sl": sl, "tp": tp, "qty": qty,
            "opened": now.isoformat(), "trail": False, "mfe": 0.0, "mae": 0.0})
        tg(f"🚀 *OPEN* {sym} {side.upper()}  entry={price:.4f}  SL={sl:.4f}  TP={tp:.4f}")

    save(st)
    print(f"done · open={len(st['positions'])} · eq=${st['equity']:.2f}")

if __name__ == "__main__":
    main()