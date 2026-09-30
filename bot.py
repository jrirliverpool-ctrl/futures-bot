"""
ApexQuant v1 — Donchian breakout bot (paper trading).
Phase: signal-collection — logs potential signals rejected by ATR filter.
"""
import os, json, csv, traceback
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
    "risk": 0.01, "sl_atr": 1.5, "tp_r": 2.0,
    "trail_act_r": 1.0, "trail_atr": 2.0,
    "fee": 0.0026, "slip": 0.0005, "max_pos": 3,
}

PAPER  = os.getenv("PAPER", "1") == "1"
STATE  = Path("state.json")
TRADES = Path("trades.csv")
SIGNALS = Path("signals_log.csv")   # ← جدید
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT  = os.getenv("TG_CHAT", "").strip()


def tg(msg):
    print(msg)
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10)
    except Exception as e:
        print(f"tg err: {e}")


def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except: pass
    return {"positions": [], "equity": 1000.0, "last_bar_ts": None,
            "last_run": None, "symbols_status": {}, "last_error": None}


def save_state(s):
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2, default=str))
    tmp.replace(STATE)


def log_trade(t):
    is_new = not TRADES.exists()
    with TRADES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(t.keys()))
        if is_new: w.writeheader()
        w.writerow(t)


def log_signal(row_data):
    """Log EVERY Donchian+EMA signal, whether or not ATR filter passed."""
    is_new = not SIGNALS.exists()
    with SIGNALS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row_data.keys()))
        if is_new: w.writeheader()
        w.writerow(row_data)


def make_exchange():
    klass = getattr(ccxt, CFG["exchange"])
    return klass({"enableRateLimit": True})


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


def fetch_closed(e, sym):
    raw = e.fetch_ohlcv(sym, CFG["tf"], limit=300)
    if not raw or len(raw) < CFG["dc"] + 20:
        raise ValueError(f"only {len(raw) if raw else 0} candles")
    raw = raw[:-1]
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")
    return indicators(df)


def raw_signal(row):
    """Return +1/-1/0 for Donchian+EMA WITHOUT ATR filter."""
    if row.close > row.dc_h and row.ema_f > row.ema_s: return 1
    if row.close < row.dc_l and row.ema_f < row.ema_s: return -1
    return 0


def atr_ok(row):
    return CFG["atr_min_pct"] <= row.atr_pct <= CFG["atr_max_pct"]


def size_position(eq, entry, sl):
    r = abs(entry - sl)
    return (eq * CFG["risk"]) / r if r > 0 else 0.0


def main():
    st  = load_state()
    now = datetime.now(timezone.utc)
    st["last_run"] = now.isoformat()
    st["last_error"] = None
    st["symbols_status"] = {}

    tg(f"🤖 ApexQuant · {now:%Y-%m-%d %H:%M UTC} · PAPER={PAPER}")

    try:
        # 1. init exchange
        try:
            e = make_exchange()
            st["symbols_status"]["__exchange_init"] = "ok"
        except Exception as ex:
            st["symbols_status"]["__exchange_init"] = f"{type(ex).__name__}: {ex}"
            st["last_error"] = f"exchange init: {ex}"
            tg(f"❌ exchange init failed: {ex}")
            save_state(st); return

        # 2. fetch
        data = {}
        for sym in CFG["symbols"]:
            try:
                df = fetch_closed(e, sym)
                data[sym] = df
                last = df.iloc[-1]
                st["symbols_status"][sym] = (
                    f"ok · {len(df)} bars · close={last.close:.2f} · "
                    f"atr%={last.atr_pct:.2f}")
            except Exception as ex:
                st["symbols_status"][sym] = f"{type(ex).__name__}: {str(ex)[:150]}"

        if not data:
            st["last_error"] = "no data from any symbol"
            tg("❌ no data — abort")
            save_state(st); return

        latest_ts = max(df.index[-1] for df in data.values())
        st["symbols_status"]["__latest_bar"] = str(latest_ts)

        # 3. idempotency
        if st.get("last_bar_ts") == str(latest_ts):
            tg(f"   bar already processed: {latest_ts}")
            save_state(st); return

        # 4. SCAN all symbols for signals (log even rejected ones)
        rejected = 0
        passed = 0
        for sym, df in data.items():
            if latest_ts not in df.index: continue
            row = df.loc[latest_ts]
            if np.isnan(row.atr) or row.atr <= 0: continue
            sig = raw_signal(row)
            if sig == 0: continue

            ok = atr_ok(row)
            log_signal({
                "time":       str(latest_ts),
                "symbol":     sym,
                "signal":     "long" if sig == 1 else "short",
                "close":      round(row.close, 4),
                "atr_pct":    round(row.atr_pct, 3),
                "atr_min":    CFG["atr_min_pct"],
                "atr_max":    CFG["atr_max_pct"],
                "passed":     int(ok),
            })
            if ok:
                passed += 1
            else:
                rejected += 1
                print(f"   ⚠️ {sym} {('long' if sig==1 else 'short')} "
                      f"REJECTED — atr%={row.atr_pct:.3f} not in "
                      f"[{CFG['atr_min_pct']},{CFG['atr_max_pct']}]")

        st["symbols_status"]["__signals_passed"]   = passed
        st["symbols_status"]["__signals_rejected"] = rejected

        # 5. EXIT PASS
        keep = []
        for p in st["positions"]:
            if p["symbol"] not in data: keep.append(p); continue
            df = data[p["symbol"]]
            if latest_ts not in df.index: keep.append(p); continue
            row = df.loc[latest_ts]
            hi, lo, entry = row.high, row.low, p["entry"]
            risk = abs(entry - p["initial_sl"])
            exit_p, why = None, None
            if p["side"] == "long":
                if lo <= p["sl"]: exit_p, why = p["sl"], "SL"
                elif hi >= p["tp"]: exit_p, why = p["tp"], "TP"
            else:
                if hi >= p["sl"]: exit_p, why = p["sl"], "SL"
                elif lo <= p["tp"]: exit_p, why = p["tp"], "TP"
            if exit_p is not None:
                sign = 1 if p["side"] == "long" else -1
                gross = sign * (exit_p - entry) * p["qty"]
                fees  = (entry + exit_p) * p["qty"] * CFG["fee"]
                pnl   = gross - fees
                r_mul = pnl / (risk * p["qty"])
                st["equity"] += pnl
                log_trade({"time": str(latest_ts), "symbol": p["symbol"],
                    "side": p["side"], "entry": round(entry,6),
                    "exit": round(exit_p,6), "qty": round(p["qty"],8),
                    "pnl": round(pnl,4), "R": round(r_mul,3), "reason": why,
                    "mae": round(p.get("mae",0),3), "mfe": round(p.get("mfe",0),3)})
                tg(f"✅ *{why}* {p['symbol']} R={r_mul:+.2f} PnL=${pnl:+.2f}")
                continue
            if p["side"] == "long":
                p["mfe"] = max(p.get("mfe",0), (hi-entry)/risk)
                p["mae"] = min(p.get("mae",0), (lo-entry)/risk)
            else:
                p["mfe"] = max(p.get("mfe",0), (entry-lo)/risk)
                p["mae"] = min(p.get("mae",0), (entry-hi)/risk)
            if not p.get("trail") and p["mfe"] >= CFG["trail_act_r"]:
                p["trail"] = True
            if p.get("trail"):
                if p["side"] == "long":
                    p["sl"] = max(p["sl"], hi - CFG["trail_atr"]*row.atr)
                else:
                    p["sl"] = min(p["sl"], lo + CFG["trail_atr"]*row.atr)
            p["bars_held"] = p.get("bars_held", 0) + 1
            keep.append(p)
        st["positions"] = keep

        # 6. ENTRY PASS (only signals that passed ATR filter)
        held = {p["symbol"] for p in st["positions"]}
        opened = 0
        for sym, df in data.items():
            if len(st["positions"]) >= CFG["max_pos"]: break
            if sym in held: continue
            if latest_ts not in df.index: continue
            row = df.loc[latest_ts]
            if np.isnan(row.atr) or row.atr <= 0: continue
            sig = raw_signal(row)
            if sig == 0 or not atr_ok(row): continue
            side  = "long" if sig == 1 else "short"
            price = row.close * (1 + CFG["slip"] * (1 if sig == 1 else -1))
            if side == "long":
                sl = price - CFG["sl_atr"]*row.atr
                tp = price + CFG["tp_r"]*CFG["sl_atr"]*row.atr
            else:
                sl = price + CFG["sl_atr"]*row.atr
                tp = price - CFG["tp_r"]*CFG["sl_atr"]*row.atr
            qty = size_position(st["equity"], price, sl)
            if qty <= 0: continue
            st["positions"].append({
                "symbol": sym, "side": side, "entry": price,
                "initial_sl": sl, "sl": sl, "tp": tp, "qty": qty,
                "opened": str(latest_ts), "trail": False,
                "mfe": 0.0, "mae": 0.0, "bars_held": 0})
            opened += 1
            tg(f"🚀 *OPEN* {sym} {side.upper()} entry={price:.4f} "
               f"SL={sl:.4f} TP={tp:.4f}")

        st["last_bar_ts"] = str(latest_ts)
        st["symbols_status"]["__opened"] = opened

    except Exception as ex:
        st["last_error"] = f"{type(ex).__name__}: {ex}"
        print(traceback.format_exc())
        tg(f"💥 crash: {ex}")

    finally:
        save_state(st)
        print(f"\ndone · open={len(st['positions'])} · eq=${st['equity']:.2f}")
        print(f"status = {json.dumps(st['symbols_status'], indent=2)}")


if __name__ == "__main__":
    main()