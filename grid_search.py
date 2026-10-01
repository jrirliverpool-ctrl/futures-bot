"""
ApexQuant — Grid search over key parameters.
Single data fetch, many simulations. Portfolio-level, fees-aware.

Grid:
  sl_atr       × tp_r        × trail_act_r × dc
  [1.0,1.5,2.0]×[2,2.5,3,4] ×[0.5,1,1.5] ×[20,40,55]
  = 108 combinations
"""
import json, time, itertools
from datetime import datetime, timezone
import ccxt
import pandas as pd
import numpy as np

BASE = {
    "exchange": "kraken",
    "symbols":  ["BTC/USD", "ETH/USD", "SOL/USD"],
    "tf":       "15m",
    "candles_total": 1000,
    "candle_limit_per_call": 720,

    "ema_f": 20, "ema_s": 50, "atr_p": 14,
    "atr_min_pct": 0.30, "atr_max_pct": 5.00,

    "risk": 0.01,
    "fee":  0.0026,
    "slip": 0.0005,

    "max_pos": 3,
    "max_per_symbol": 1,

    "equity0": 1000.0,
}

GRID = {
    "sl_atr":       [1.0, 1.5, 2.0],
    "tp_r":         [2.0, 2.5, 3.0, 4.0],
    "trail_act_r":  [0.5, 1.0, 1.5],
    "dc":           [20, 40, 55],
}


# ═══════════════════════════════════════════════════════════════
def make_exchange():
    return getattr(ccxt, BASE["exchange"])({"enableRateLimit": True})


def fetch_paginated(e, sym, tf, target):
    tf_ms = e.parse_timeframe(tf) * 1000
    now_ms = e.milliseconds()
    since = now_ms - target * tf_ms * 2
    collected = []
    while len(collected) < target * 2:
        try:
            batch = e.fetch_ohlcv(sym, tf, since=since,
                                  limit=BASE["candle_limit_per_call"])
        except Exception as ex:
            print(f"    fetch error: {type(ex).__name__}: {ex}")
            break
        if not batch: break
        collected.extend(batch)
        since = batch[-1][0] + tf_ms
        if len(batch) < BASE["candle_limit_per_call"]: break
        time.sleep(0.4)
    seen, out = set(), []
    for r in collected:
        if r[0] in seen: continue
        seen.add(r[0]); out.append(r)
    out.sort(key=lambda x: x[0])
    return out[-target:] if len(out) > target else out


def fetch_raw(e, sym):
    raw = fetch_paginated(e, sym, BASE["tf"], BASE["candles_total"])
    if len(raw) < 80: raise ValueError(f"only {len(raw)} candles")
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    return df


def add_indicators(df, dc):
    df = df.copy()
    df["ema_f"] = df.close.ewm(span=BASE["ema_f"], adjust=False).mean()
    df["ema_s"] = df.close.ewm(span=BASE["ema_s"], adjust=False).mean()
    tr = pd.concat([df.high-df.low, (df.high-df.close.shift()).abs(),
                    (df.low-df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr"]     = tr.ewm(alpha=1/BASE["atr_p"], adjust=False).mean()
    df["atr_pct"] = df.atr / df.close * 100
    df["dc_h"]    = df.high.rolling(dc).max().shift(1)
    df["dc_l"]    = df.low.rolling(dc).min().shift(1)
    return df


def signal_from_row(row, cfg):
    if pd.isna(row.atr_pct): return 0
    if row.atr_pct < BASE["atr_min_pct"] or row.atr_pct > BASE["atr_max_pct"]:
        return 0
    if row.close > row.dc_h and row.ema_f > row.ema_s: return 1
    if row.close < row.dc_l and row.ema_f < row.ema_s: return -1
    return 0


# ═══════════════════════════════════════════════════════════════
def simulate(dfs, cfg):
    all_ts = sorted(set().union(*[set(d.index) for d in dfs.values()]))
    equity = BASE["equity0"]
    open_positions = {}
    trades = []

    for ts in all_ts:
        # ── A. Manage exits ───────────────────────────
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df  = dfs[sym]
            if ts not in df.index: continue
            bar = df.loc[ts]
            hi, lo = float(bar.high), float(bar.low)
            entry  = pos["ideal_entry"]
            risk_u = abs(entry - pos["initial_sl"])

            if pos["side"] == "long":
                pos["mfe"] = max(pos["mfe"], (hi-entry)/risk_u)
                pos["mae"] = min(pos["mae"], (lo-entry)/risk_u)
            else:
                pos["mfe"] = max(pos["mfe"], (entry-lo)/risk_u)
                pos["mae"] = min(pos["mae"], (entry-hi)/risk_u)

            # Conservative same-bar SL-first
            exit_p, why = None, None
            if pos["side"] == "long":
                if lo <= pos["sl"]:   exit_p, why = pos["sl"], "SL"
                elif hi >= pos["tp"]: exit_p, why = pos["tp"], "TP"
            else:
                if hi >= pos["sl"]:   exit_p, why = pos["sl"], "SL"
                elif lo <= pos["tp"]: exit_p, why = pos["tp"], "TP"

            if exit_p is None:
                if not pos["trail"] and pos["mfe"] >= cfg["trail_act_r"]:
                    pos["trail"] = True
                if pos["trail"]:
                    if pos["side"] == "long":
                        pos["sl"] = max(pos["sl"], hi - BASE.get("trail_atr", 2.0)*float(bar.atr))
                    else:
                        pos["sl"] = min(pos["sl"], lo + BASE.get("trail_atr", 2.0)*float(bar.atr))
                pos["bars_held"] += 1
                continue

            sign = 1 if pos["side"] == "long" else -1
            ideal_exit = exit_p
            if sign == 1:
                actual_entry = entry      * (1 + BASE["slip"])
                actual_exit  = ideal_exit * (1 - BASE["slip"])
            else:
                actual_entry = entry      * (1 - BASE["slip"])
                actual_exit  = ideal_exit * (1 + BASE["slip"])

            qty = pos["qty"]
            ideal_pnl = sign * (ideal_exit  - entry)        * qty
            gross_pnl = sign * (actual_exit - actual_entry) * qty
            fees      = (actual_entry + actual_exit) * qty * BASE["fee"]
            net_pnl   = gross_pnl - fees
            equity   += net_pnl

            trades.append({
                "symbol": sym, "side": pos["side"],
                "reason": why, "bars": pos["bars_held"]+1,
                "net_pnl": net_pnl, "gross_pnl": gross_pnl,
                "fees": fees, "R_net": net_pnl/(risk_u*qty),
                "mae_R": pos["mae"], "mfe_R": pos["mfe"],
                "equity_after": equity,
            })
            del open_positions[sym]

        # ── B. New entries ────────────────────────────
        if len(open_positions) < BASE["max_pos"]:
            for sym, df in dfs.items():
                if len(open_positions) >= BASE["max_pos"]: break
                if sym in open_positions: continue
                if ts not in df.index: continue
                bar = df.loc[ts]
                if pd.isna(bar.atr) or bar.atr <= 0: continue
                sig = signal_from_row(bar, cfg)
                if sig == 0: continue
                n_sym = sum(1 for p in open_positions.values() if p["symbol"] == sym)
                if n_sym >= BASE["max_per_symbol"]: continue

                side = "long" if sig == 1 else "short"
                ie = float(bar.close); atr = float(bar.atr)
                if side == "long":
                    sl = ie - cfg["sl_atr"]*atr
                    tp = ie + cfg["tp_r"]*cfg["sl_atr"]*atr
                else:
                    sl = ie + cfg["sl_atr"]*atr
                    tp = ie - cfg["tp_r"]*cfg["sl_atr"]*atr
                risk_u = abs(ie - sl)
                if risk_u <= 0: continue
                qty = (equity * BASE["risk"]) / risk_u
                if qty <= 0: continue

                open_positions[sym] = {
                    "symbol": sym, "side": side, "ideal_entry": ie,
                    "initial_sl": sl, "sl": sl, "tp": tp, "qty": qty,
                    "trail": False, "mfe": 0.0, "mae": 0.0, "bars_held": 0,
                }

    return trades, equity


def summarize(trades):
    if not trades:
        return {"trades": 0}
    td = pd.DataFrame(trades)
    eq = td["equity_after"].values
    dd = float((eq/np.maximum.accumulate(eq) - 1).min())
    w = td[td.net_pnl > 0]; l = td[td.net_pnl <= 0]
    return {
        "trades":         int(len(td)),
        "win_rate_pct":   round(len(w)/len(td)*100, 2),
        "gross_pnl":      round(float(td.gross_pnl.sum()), 2),
        "fees":           round(float(td.fees.sum()), 2),
        "net_pnl":        round(float(td.net_pnl.sum()), 2),
        "total_R_net":    round(float(td.R_net.sum()), 2),
        "expectancy_R":   round(float(td.R_net.mean()), 3),
        "avg_win_R":      round(float(w.R_net.mean()), 3) if len(w) else None,
        "avg_loss_R":     round(float(l.R_net.mean()), 3) if len(l) else None,
        "avg_MAE_win":    round(float(w.mae_R.mean()), 3) if len(w) else None,
        "avg_MFE_win":    round(float(w.mfe_R.mean()), 3) if len(w) else None,
        "max_dd_pct":     round(dd*100, 2),
        "final_eq":       round(float(eq[-1]), 2),
        "sl_count":       int((td.reason == "SL").sum()),
        "tp_count":       int((td.reason == "TP").sum()),
    }


# ═══════════════════════════════════════════════════════════════
def main():
    e = make_exchange()
    print(f"Fetching raw data once ...")
    raw = {}
    for sym in BASE["symbols"]:
        try:
            raw[sym] = fetch_raw(e, sym)
            print(f"  ✓ {sym}: {len(raw[sym])} bars  "
                  f"{raw[sym].index[0]} → {raw[sym].index[-1]}")
        except Exception as ex:
            print(f"  ✗ {sym}: {ex}")

    if not raw:
        print("No data. Abort."); return

    combos = list(itertools.product(
        GRID["sl_atr"], GRID["tp_r"], GRID["trail_act_r"], GRID["dc"]))
    print(f"\nRunning {len(combos)} combinations...\n")

    results = []
    for i, (sl_atr, tp_r, trail_act_r, dc) in enumerate(combos, 1):
        cfg = {"sl_atr": sl_atr, "tp_r": tp_r,
               "trail_act_r": trail_act_r, "dc": dc}
        dfs = {sym: add_indicators(df, dc) for sym, df in raw.items()}
        trades, final_eq = simulate(dfs, cfg)
        s = summarize(trades)
        s.update(cfg)
        results.append(s)

        if i % 12 == 0:
            print(f"  ... {i}/{len(combos)} done")

    df = pd.DataFrame(results)

    # Sort by net expectancy; save full grid + top 10
    df = df.sort_values("expectancy_R", ascending=False).reset_index(drop=True)
    df.to_csv("grid_results.csv", index=False)

    with open("grid_report.json", "w") as f:
        json.dump({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "base":   BASE,
            "grid":   GRID,
            "combos": len(combos),
            "top20":  df.head(20).to_dict("records"),
        }, f, indent=2, default=str)

    print("\n══════════ TOP 15 COMBINATIONS (by expectancy R) ══════════")
    cols = ["sl_atr","tp_r","trail_act_r","dc","trades","win_rate_pct",
            "expectancy_R","total_R_net","net_pnl","max_dd_pct"]
    print(df[cols].head(15).to_string(index=False))

    print(f"\nBest expectancy: {df.iloc[0]['expectancy_R']:+.3f} R")
    print(f"Worst expectancy: {df.iloc[-1]['expectancy_R']:+.3f} R")
    print(f"How many combos have positive expectancy: "
          f"{(df['expectancy_R']>0).sum()}/{len(df)}")
    print("\n✓ grid_results.csv")
    print("✓ grid_report.json")


if __name__ == "__main__":
    main()