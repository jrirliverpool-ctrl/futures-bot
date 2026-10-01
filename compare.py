"""
ApexQuant — Matrix comparison (TF × strategy × fee).
Mirrors backtest.py exactly in simulation logic; only paramterizes CFG.
Strategies: donchian (baseline) vs meanrev (BB+RSI contrarian).
Fee scenarios: kraken (0.26%) vs lowfee (0.055% hypothetical).
"""
import json, time
from datetime import datetime, timezone
from itertools import product
import ccxt
import pandas as pd
import numpy as np

BASE = {
    "exchange": "kraken",
    "symbols": ["BTC/USD", "ETH/USD", "SOL/USD"],
    "candles_total": 720,
    "candle_limit_per_call": 720,

    "ema_f": 20, "ema_s": 50, "atr_p": 14,
    "atr_min_pct": 0.30, "atr_max_pct": 5.00,

    # risk params (fixed across matrix — same as backtest.py baseline)
    "dc": 20,
    "risk": 0.01,
    "sl_atr": 1.5,
    "tp_r": 2.0,
    "trail_act_r": 1.0,
    "trail_atr": 2.0,
    "slip": 0.0005,
    "max_pos": 3,
    "max_per_symbol": 1,
    "equity0": 1000.0,

    # mean reversion params
    "bb_p": 20, "bb_std": 2.0,
    "rsi_p": 14, "rsi_os": 30, "rsi_ob": 70,
}

MATRIX = {
    "tf":       ["15m", "1h", "4h"],
    "strategy": ["donchian", "meanrev"],
    "fee":      {"kraken": 0.0026, "lowfee": 0.00055},
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


def load_symbol(e, sym, tf):
    raw = fetch_paginated(e, sym, tf, BASE["candles_total"])
    if len(raw) < BASE["dc"] + 60:
        raise ValueError(f"only {len(raw)} candles")
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df = df.iloc[:-1]   # drop still-forming candle (same as bot.py)
    return df


def add_indicators(df):
    """All indicators added once. Both strategies use them."""
    df = df.copy()
    # ATR
    tr = pd.concat([df.high-df.low, (df.high-df.close.shift()).abs(),
                    (df.low-df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr"]     = tr.ewm(alpha=1/BASE["atr_p"], adjust=False).mean()
    df["atr_pct"] = df.atr / df.close * 100

    # EMA + Donchian
    df["ema_f"] = df.close.ewm(span=BASE["ema_f"], adjust=False).mean()
    df["ema_s"] = df.close.ewm(span=BASE["ema_s"], adjust=False).mean()
    df["dc_h"]  = df.high.rolling(BASE["dc"]).max().shift(1)
    df["dc_l"]  = df.low.rolling(BASE["dc"]).min().shift(1)

    # Bollinger
    ma = df.close.rolling(BASE["bb_p"]).mean()
    sd = df.close.rolling(BASE["bb_p"]).std()
    df["bb_mid"] = ma
    df["bb_up"]  = ma + BASE["bb_std"] * sd
    df["bb_dn"]  = ma - BASE["bb_std"] * sd

    # RSI (Wilder)
    d  = df.close.diff()
    g  = d.clip(lower=0)
    l  = -d.clip(upper=0)
    ag = g.ewm(alpha=1/BASE["rsi_p"], adjust=False).mean()
    al = l.ewm(alpha=1/BASE["rsi_p"], adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    df["rsi"] = 100 - 100/(1+rs)
    return df


def signal_from_row(row, strategy):
    if pd.isna(row.atr_pct): return 0
    if row.atr_pct < BASE["atr_min_pct"] or row.atr_pct > BASE["atr_max_pct"]:
        return 0

    if strategy == "donchian":
        if pd.isna(row.dc_h) or pd.isna(row.dc_l): return 0
        if row.close > row.dc_h and row.ema_f > row.ema_s: return 1
        if row.close < row.dc_l and row.ema_f < row.ema_s: return -1
        return 0

    if strategy == "meanrev":
        if pd.isna(row.bb_up) or pd.isna(row.rsi): return 0
        if row.close < row.bb_dn and row.rsi < BASE["rsi_os"]: return 1
        if row.close > row.bb_up and row.rsi > BASE["rsi_ob"]: return -1
        return 0

    return 0


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO SIMULATION — mirrors backtest.py exactly, cfg passed in
# ═══════════════════════════════════════════════════════════════
def simulate(dfs, cfg, strategy):
    all_ts = sorted(set().union(*[set(d.index) for d in dfs.values()]))
    equity = BASE["equity0"]
    open_positions = {}
    trades = []

    for ts in all_ts:
        # ── (A) MANAGE EXITS ─────────────────────────────
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df  = dfs[sym]
            if ts not in df.index: continue
            bar = df.loc[ts]
            hi, lo = float(bar.high), float(bar.low)
            entry  = pos["ideal_entry"]
            risk_u = abs(entry - pos["initial_sl"])

            if pos["side"] == "long":
                pos["mfe"] = max(pos["mfe"], (hi - entry) / risk_u)
                pos["mae"] = min(pos["mae"], (lo - entry) / risk_u)
            else:
                pos["mfe"] = max(pos["mfe"], (entry - lo) / risk_u)
                pos["mae"] = min(pos["mae"], (entry - hi) / risk_u)

            # conservative same-bar: SL first
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
                        pos["sl"] = max(pos["sl"], hi - cfg["trail_atr"]*float(bar.atr))
                    else:
                        pos["sl"] = min(pos["sl"], lo + cfg["trail_atr"]*float(bar.atr))
                pos["bars_held"] += 1
                continue

            sign = 1 if pos["side"] == "long" else -1
            ideal_exit = exit_p
            if sign == 1:
                ae = entry      * (1 + cfg["slip"])
                ax = ideal_exit * (1 - cfg["slip"])
            else:
                ae = entry      * (1 - cfg["slip"])
                ax = ideal_exit * (1 + cfg["slip"])

            qty = pos["qty"]
            ideal_pnl = sign * (ideal_exit - entry) * qty
            gross_pnl = sign * (ax - ae) * qty
            fees      = (ae + ax) * qty * cfg["fee"]
            net_pnl   = gross_pnl - fees
            slip_cost = ideal_pnl - gross_pnl
            equity   += net_pnl

            trades.append({
                "symbol": sym, "side": pos["side"],
                "entry_time": pos["entry_time"], "exit_time": str(ts),
                "ideal_entry": entry, "ideal_exit": ideal_exit,
                "actual_entry": ae, "actual_exit": ax,
                "sl_initial": pos["initial_sl"], "tp": pos["tp"],
                "qty": qty, "reason": why,
                "bars_held": pos["bars_held"] + 1,
                "ideal_pnl": ideal_pnl, "gross_pnl": gross_pnl,
                "fees": fees, "slip_cost": slip_cost, "net_pnl": net_pnl,
                "R_net": net_pnl / (risk_u * qty),
                "mfe_R": pos["mfe"], "mae_R": pos["mae"],
                "equity_after": equity,
            })
            del open_positions[sym]

        # ── (B) NEW ENTRIES ─────────────────────────────
        if len(open_positions) < cfg["max_pos"]:
            for sym, df in dfs.items():
                if len(open_positions) >= cfg["max_pos"]: break
                if sym in open_positions: continue
                if ts not in df.index: continue
                bar = df.loc[ts]
                if pd.isna(bar.atr) or bar.atr <= 0: continue
                sig = signal_from_row(bar, strategy)
                if sig == 0: continue
                n_sym = sum(1 for p in open_positions.values() if p["symbol"] == sym)
                if n_sym >= cfg["max_per_symbol"]: continue

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
                qty = (equity * cfg["risk"]) / risk_u
                if qty <= 0: continue

                open_positions[sym] = {
                    "symbol": sym, "side": side,
                    "ideal_entry": ie, "entry_time": str(ts),
                    "initial_sl": sl, "sl": sl, "tp": tp, "qty": qty,
                    "trail": False, "mfe": 0.0, "mae": 0.0, "bars_held": 0,
                }

    return trades, equity


def stats(trades):
    if not trades:
        return {"overall": {"trades": 0}}
    td = pd.DataFrame(trades)
    eq = td["equity_after"].values
    dd = float((eq/np.maximum.accumulate(eq) - 1).min())

    def _sub(subset):
        if len(subset) == 0: return {}
        w = subset[subset.net_pnl > 0]
        l = subset[subset.net_pnl <= 0]
        return {
            "trades":            int(len(subset)),
            "win_rate_pct":      round(len(w)/len(subset)*100, 2),
            "gross_pnl":         round(float(subset.gross_pnl.sum()), 4),
            "fees":              round(float(subset.fees.sum()), 4),
            "slip_cost":         round(float(subset.slip_cost.sum()), 4),
            "net_pnl":           round(float(subset.net_pnl.sum()), 4),
            "total_R_net":       round(float(subset.R_net.sum()), 4),
            "expectancy_R_net":  round(float(subset.R_net.mean()), 4),
            "avg_win_R_net":     round(float(w.R_net.mean()), 3) if len(w) else None,
            "avg_loss_R_net":    round(float(l.R_net.mean()), 3) if len(l) else None,
            "avg_MAE_R_winners": round(float(w.mae_R.mean()), 3) if len(w) else None,
            "avg_MFE_R_winners": round(float(w.mfe_R.mean()), 3) if len(w) else None,
            "avg_bars_held":     round(float(subset.bars_held.mean()), 1),
        }

    return {
        "overall":      _sub(td),
        "by_reason":    {str(k): int(v) for k, v in td.reason.value_counts().items()},
        "by_side":      {str(k): int(v) for k, v in td.side.value_counts().items()},
        "by_symbol":    {str(s): _sub(td[td.symbol == s]) for s in td.symbol.unique()},
        "max_dd_pct":   round(dd*100, 3),
        "final_equity": round(float(eq[-1]), 2),
    }


# ═══════════════════════════════════════════════════════════════
def main():
    e = make_exchange()
    results = []

    for tf in MATRIX["tf"]:
        print(f"\n────────── TF = {tf} ──────────")
        dfs = {}
        for sym in BASE["symbols"]:
            try:
                dfs[sym] = add_indicators(load_symbol(e, sym, tf))
                print(f"  ✓ {sym}: {len(dfs[sym])} bars  "
                      f"{dfs[sym].index[0]} → {dfs[sym].index[-1]}")
            except Exception as ex:
                print(f"  ✗ {sym}: {type(ex).__name__}: {ex}")
        if not dfs:
            continue

        for strategy, (fee_name, fee_val) in product(
                MATRIX["strategy"], MATRIX["fee"].items()):

            cfg = {
                "fee": fee_val,
                "slip": BASE["slip"],
                "risk": BASE["risk"],
                "sl_atr": BASE["sl_atr"],
                "tp_r": BASE["tp_r"],
                "trail_act_r": BASE["trail_act_r"],
                "trail_atr": BASE["trail_atr"],
                "max_pos": BASE["max_pos"],
                "max_per_symbol": BASE["max_per_symbol"],
            }
            trades, eq = simulate(dfs, cfg, strategy)

            # walk-forward split (same as backtest.py)
            if trades:
                td = pd.DataFrame(trades)
                all_t = sorted(td.entry_time.astype(str).unique())
                n = len(all_t); si = int(n * 0.60)
                split_ts = all_t[si] if si < n else all_t[-1]
                is_t  = td[td.entry_time.astype(str) <  split_ts].to_dict("records")
                oos_t = td[td.entry_time.astype(str) >= split_ts].to_dict("records")
            else:
                is_t, oos_t = [], []

            s_full = stats(trades)
            s_is   = stats(is_t)
            s_oos  = stats(oos_t)

            row = {
                "tf": tf, "strategy": strategy, "fee_name": fee_name,
                "fee_pct": fee_val * 100,
                **{f"full_{k}": v for k, v in s_full.get("overall", {}).items()},
                "full_max_dd": s_full.get("max_dd_pct"),
                "is_trades":   s_is.get("overall", {}).get("trades", 0),
                "is_expect":   s_is.get("overall", {}).get("expectancy_R_net"),
                "oos_trades":  s_oos.get("overall", {}).get("trades", 0),
                "oos_expect":  s_oos.get("overall", {}).get("expectancy_R_net"),
            }
            results.append(row)

            if s_full.get("overall", {}).get("trades"):
                o = s_full["overall"]
                print(f"    {strategy:9s} fee={fee_name:7s} → "
                      f"n={o['trades']:3d}  WR={o['win_rate_pct']:5.1f}%  "
                      f"exp={o['expectancy_R_net']:+.3f}R  "
                      f"net=${o['net_pnl']:+.2f}  "
                      f"IS/OOS={row['is_expect']}/{row['oos_expect']}")
            else:
                print(f"    {strategy:9s} fee={fee_name:7s} → no trades")

    df = pd.DataFrame(results)
    df.to_csv("compare_results.csv", index=False)
    with open("compare_report.json", "w") as f:
        json.dump({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "base":   BASE,
            "matrix": MATRIX,
            "results": df.to_dict("records"),
        }, f, indent=2, default=str)

    print("\n══════════ FULL MATRIX ══════════")
    show_cols = ["tf","strategy","fee_name","full_trades","full_win_rate_pct",
                 "full_expectancy_R_net","full_net_pnl","full_max_dd",
                 "is_expect","oos_expect"]
    print(df[[c for c in show_cols if c in df.columns]].to_string(index=False))
    print("\n✓ compare_results.csv")
    print("✓ compare_report.json")


if __name__ == "__main__":
    main()