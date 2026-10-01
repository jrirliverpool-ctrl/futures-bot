"""
ApexQuant — Portfolio-level backtest with walk-forward split.

Design choices:
  • Portfolio simulation: max_pos total, max 1 per symbol
  • No look-ahead: Donchian shifted, signal only from CLOSED bars
  • Conservative same-bar: if both SL and TP touched, assume SL first
  • Trailing: SL updated using current bar's high/low, used NEXT bar
  • Fees and slippage reported separately from gross PnL
  • Walk-forward split: 60% in-sample (IS), 40% out-of-sample (OOS)
"""
import json, time
from datetime import datetime, timezone
import ccxt
import pandas as pd
import numpy as np

CFG = {
    "exchange": "kraken",
    "symbols": ["BTC/USD", "ETH/USD", "SOL/USD"],
    "tf": "15m",
    "candles_total": 1000,          # target per symbol
    "candle_limit_per_call": 720,   # Kraken API hard limit

    "dc": 20, "ema_f": 20, "ema_s": 50, "atr_p": 14,
    "atr_min_pct": 0.30, "atr_max_pct": 5.00,

    "risk": 0.01,
    "sl_atr": 1.5,
    "tp_r": 2.0,
    "trail_act_r": 1.0,
    "trail_atr": 2.0,

    "fee": 0.0026,      # Kraken taker spot (report as-is; adjustable)
    "slip": 0.0005,

    "max_pos": 3,
    "max_per_symbol": 1,

    "equity0": 1000.0,
    "is_split": 0.60,   # first 60% in-sample
}


# ═══════════════════════════════════════════════════════════════
def make_exchange():
    return getattr(ccxt, CFG["exchange"])({"enableRateLimit": True})


def fetch_paginated(e, sym, tf, target):
    """Kraken caps at 720/call. Paginate forward until we have `target`."""
    tf_ms = e.parse_timeframe(tf) * 1000
    now_ms = e.milliseconds()
    since = now_ms - target * tf_ms * 2   # ask for extra to be safe
    collected = []
    while len(collected) < target * 2:
        try:
            batch = e.fetch_ohlcv(sym, tf, since=since, limit=CFG["candle_limit_per_call"])
        except Exception as ex:
            print(f"    fetch error: {type(ex).__name__}: {ex}")
            break
        if not batch:
            break
        collected.extend(batch)
        last_ts = batch[-1][0]
        since = last_ts + tf_ms
        if len(batch) < CFG["candle_limit_per_call"]:
            break
        time.sleep(0.4)   # rate limit
    # dedupe & sort
    seen, out = set(), []
    for r in collected:
        if r[0] in seen: continue
        seen.add(r[0]); out.append(r)
    out.sort(key=lambda x: x[0])
    return out[-target:] if len(out) > target else out


def load_symbol(e, sym):
    raw = fetch_paginated(e, sym, CFG["tf"], CFG["candles_total"])
    if len(raw) < CFG["dc"] + 60:
        raise ValueError(f"only {len(raw)} candles")
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    return df


def add_indicators(df):
    df = df.copy()
    df["ema_f"] = df.close.ewm(span=CFG["ema_f"], adjust=False).mean()
    df["ema_s"] = df.close.ewm(span=CFG["ema_s"], adjust=False).mean()
    tr = pd.concat([df.high-df.low, (df.high-df.close.shift()).abs(),
                    (df.low-df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr"]     = tr.ewm(alpha=1/CFG["atr_p"], adjust=False).mean()
    df["atr_pct"] = df.atr / df.close * 100
    df["dc_h"]    = df.high.rolling(CFG["dc"]).max().shift(1)
    df["dc_l"]    = df.low.rolling(CFG["dc"]).min().shift(1)
    return df


def signal_from_row(row):
    if pd.isna(row.atr_pct): return 0
    if row.atr_pct < CFG["atr_min_pct"] or row.atr_pct > CFG["atr_max_pct"]:
        return 0
    if row.close > row.dc_h and row.ema_f > row.ema_s: return 1
    if row.close < row.dc_l and row.ema_f < row.ema_s: return -1
    return 0


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO SIMULATION
# ═══════════════════════════════════════════════════════════════
def simulate(dfs):
    """
    dfs: dict {symbol: DataFrame-with-indicators}, aligned on union index.
    Returns: list of trade dicts.
    """
    # Build union timeline
    all_ts = sorted(set().union(*[set(d.index) for d in dfs.values()]))
    equity = CFG["equity0"]
    open_positions = {}     # symbol -> dict
    trades = []

    for ts in all_ts:
        # ── (A) MANAGE EXITS: use SL/TP as they were set BEFORE this bar ──
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df  = dfs[sym]
            if ts not in df.index:
                continue
            bar = df.loc[ts]
            hi, lo = float(bar.high), float(bar.low)
            entry  = pos["entry"]
            risk_u = abs(entry - pos["initial_sl"])

            # MFE / MAE using this bar's range
            if pos["side"] == "long":
                pos["mfe"] = max(pos["mfe"], (hi - entry) / risk_u)
                pos["mae"] = min(pos["mae"], (lo - entry) / risk_u)
            else:
                pos["mfe"] = max(pos["mfe"], (entry - lo) / risk_u)
                pos["mae"] = min(pos["mae"], (entry - hi) / risk_u)

            # Conservative same-bar: SL FIRST, then TP
            exit_p, why = None, None
            if pos["side"] == "long":
                if lo <= pos["sl"]:      exit_p, why = pos["sl"], "SL"
                elif hi >= pos["tp"]:    exit_p, why = pos["tp"], "TP"
            else:
                if hi >= pos["sl"]:      exit_p, why = pos["sl"], "SL"
                elif lo <= pos["tp"]:    exit_p, why = pos["tp"], "TP"

            # If reached here, position still open → update trailing AFTER exit check
            if exit_p is None:
                if not pos["trail"] and pos["mfe"] >= CFG["trail_act_r"]:
                    pos["trail"] = True
                if pos["trail"]:
                    if pos["side"] == "long":
                        pos["sl"] = max(pos["sl"], hi - CFG["trail_atr"] * float(bar.atr))
                    else:
                        pos["sl"] = min(pos["sl"], lo + CFG["trail_atr"] * float(bar.atr))
                pos["bars_held"] += 1
                continue

            # ── Close position ──────────────────────────
            sign = 1 if pos["side"] == "long" else -1
            ideal_entry = pos["ideal_entry"]
            ideal_exit  = exit_p

            # Apply slippage to actual fill prices
            if sign == 1:
                actual_entry = ideal_entry * (1 + CFG["slip"])
                actual_exit  = ideal_exit  * (1 - CFG["slip"])
            else:
                actual_entry = ideal_entry * (1 - CFG["slip"])
                actual_exit  = ideal_exit  * (1 + CFG["slip"])

            qty = pos["qty"]

            # Three-layer PnL
            ideal_pnl = sign * (ideal_exit  - ideal_entry) * qty
            gross_pnl = sign * (actual_exit - actual_entry) * qty   # after slippage
            fees      = (actual_entry + actual_exit) * qty * CFG["fee"]
            net_pnl   = gross_pnl - fees
            slip_cost = ideal_pnl - gross_pnl

            equity += net_pnl

            trades.append({
                "symbol":       sym,
                "side":         pos["side"],
                "entry_time":   pos["entry_time"],
                "exit_time":    str(ts),
                "ideal_entry":  round(ideal_entry, 6),
                "ideal_exit":   round(ideal_exit, 6),
                "actual_entry": round(actual_entry, 6),
                "actual_exit":  round(actual_exit, 6),
                "sl_initial":   round(pos["initial_sl"], 6),
                "tp":           round(pos["tp"], 6),
                "qty":          round(qty, 8),
                "reason":       why,
                "bars_held":    pos["bars_held"] + 1,
                "ideal_pnl":    round(ideal_pnl, 6),
                "gross_pnl":    round(gross_pnl, 6),
                "fees":         round(fees, 6),
                "slip_cost":    round(slip_cost, 6),
                "net_pnl":      round(net_pnl, 6),
                "R_ideal":      round(ideal_pnl / (risk_u * qty), 4),
                "R_net":        round(net_pnl   / (risk_u * qty), 4),
                "mfe_R":        round(pos["mfe"], 3),
                "mae_R":        round(pos["mae"], 3),
                "equity_after": round(equity, 2),
            })
            del open_positions[sym]

        # ── (B) NEW ENTRIES: signal from CLOSED bar `ts` ──
        if len(open_positions) < CFG["max_pos"]:
            # count per-symbol
            for sym, df in dfs.items():
                if len(open_positions) >= CFG["max_pos"]:
                    break
                if sym in open_positions:
                    continue
                if ts not in df.index:
                    continue
                bar = df.loc[ts]
                if pd.isna(bar.atr) or bar.atr <= 0:
                    continue
                sig = signal_from_row(bar)
                if sig == 0:
                    continue
                # enforce max_per_symbol
                n_sym = sum(1 for p in open_positions.values() if p["symbol"] == sym)
                if n_sym >= CFG["max_per_symbol"]:
                    continue

                side = "long" if sig == 1 else "short"
                ideal_entry = float(bar.close)
                atr = float(bar.atr)
                if side == "long":
                    sl = ideal_entry - CFG["sl_atr"] * atr
                    tp = ideal_entry + CFG["tp_r"] * CFG["sl_atr"] * atr
                else:
                    sl = ideal_entry + CFG["sl_atr"] * atr
                    tp = ideal_entry - CFG["tp_r"] * CFG["sl_atr"] * atr
                risk_u = abs(ideal_entry - sl)
                if risk_u <= 0:
                    continue

                qty = (equity * CFG["risk"]) / risk_u
                if qty <= 0:
                    continue

                open_positions[sym] = {
                    "symbol": sym, "side": side,
                    "ideal_entry": ideal_entry,
                    "entry_time": str(ts),
                    "initial_sl": sl, "sl": sl, "tp": tp,
                    "qty": qty,
                    "trail": False, "mfe": 0.0, "mae": 0.0,
                    "bars_held": 0,
                }

    return trades, equity


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════
def stats(trades):
    if not trades:
        return {"total_trades": 0}
    td = pd.DataFrame(trades)
    wins  = td[td.net_pnl > 0]
    loses = td[td.net_pnl <= 0]
    eq_curve = td["equity_after"].values
    dd = float((eq_curve / np.maximum.accumulate(eq_curve) - 1).min())

    def _stats_for(subset, label):
        if len(subset) == 0: return {}
        w = subset[subset.net_pnl > 0]
        l = subset[subset.net_pnl <= 0]
        return {
            "trades":            int(len(subset)),
            "win_rate_pct":      round(len(w)/len(subset)*100, 2),
            "gross_pnl":         round(subset.gross_pnl.sum(), 4),
            "fees":              round(subset.fees.sum(), 4),
            "slip_cost":         round(subset.slip_cost.sum(), 4),
            "net_pnl":           round(subset.net_pnl.sum(), 4),
            "total_R_net":       round(subset.R_net.sum(), 4),
            "avg_R_net":         round(subset.R_net.mean(), 4),
            "expectancy_R_net":  round(subset.R_net.mean(), 4),
            "avg_win_R_net":     round(w.R_net.mean(), 3) if len(w) else None,
            "avg_loss_R_net":    round(l.R_net.mean(), 3) if len(l) else None,
            "avg_MAE_R_winners": round(w.mae_R.mean(), 3) if len(w) else None,
            "avg_MFE_R_winners": round(w.mfe_R.mean(), 3) if len(w) else None,
            "avg_bars_held":     round(subset.bars_held.mean(), 1),
        }

    by_reason = td.reason.value_counts().to_dict()
    by_side   = td.side.value_counts().to_dict()
    by_symbol = {s: _stats_for(td[td.symbol == s], s) for s in td.symbol.unique()}

    return {
        "overall": _stats_for(td, "full"),
        "by_reason": by_reason,
        "by_side": by_side,
        "by_symbol": by_symbol,
        "max_dd_pct": round(dd*100, 3),
        "final_equity": round(float(eq_curve[-1]), 2),
    }


# ═══════════════════════════════════════════════════════════════
def main():
    e = make_exchange()
    print(f"Fetching from {CFG['exchange']} · {CFG['tf']} · target {CFG['candles_total']} bars/symbol")
    dfs = {}
    for sym in CFG["symbols"]:
        try:
            df = add_indicators(load_symbol(e, sym))
            dfs[sym] = df
            print(f"  ✓ {sym}: {len(df)} bars  {df.index[0]} → {df.index[-1]}")
        except Exception as ex:
            print(f"  ✗ {sym}: {type(ex).__name__}: {ex}")

    if not dfs:
        print("No data. Abort.")
        return

    print("\nRunning portfolio simulation...")
    trades, final_eq = simulate(dfs)
    print(f"  → {len(trades)} trades, final equity ${final_eq:.2f}")

    if not trades:
        print("No trades generated. Report skipped.")
        return

    td = pd.DataFrame(trades)
    td.to_csv("backtest_trades.csv", index=False)

    # Walk-forward split by entry_time
    all_times = sorted(td.entry_time.astype(str).unique())
    n = len(all_times)
    split_idx = int(n * CFG["is_split"])
    split_ts = all_times[split_idx] if split_idx < n else all_times[-1]

    is_trades  = td[td.entry_time.astype(str) <  split_ts].to_dict("records")
    oos_trades = td[td.entry_time.astype(str) >= split_ts].to_dict("records")

    report = {
        "run_at":       datetime.now(timezone.utc).isoformat(),
        "config":       CFG,
        "data_range":   {
            "first_bar": str(min(d.index[0] for d in dfs.values())),
            "last_bar":  str(max(d.index[-1] for d in dfs.values())),
            "bars":      {s: len(d) for s, d in dfs.items()},
        },
        "split_time":   split_ts,
        "full":         stats(trades),
        "in_sample":    stats(is_trades),
        "out_of_sample":stats(oos_trades),
    }
    with open("backtest_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    # pretty print
    def _p(title, s):
        if not s.get("total_trades"):
            print(f"\n═══ {title} ═══  (no trades)")
            return
        o = s["overall"]
        print(f"\n═══ {title} ═══")
        print(f"  trades        : {o['trades']}")
        print(f"  win rate      : {o['win_rate_pct']}%")
        print(f"  gross pnl     : ${o['gross_pnl']:.2f}")
        print(f"  slippage cost : ${o['slip_cost']:.2f}")
        print(f"  fees          : ${o['fees']:.2f}")
        print(f"  net pnl       : ${o['net_pnl']:.2f}")
        print(f"  total R net   : {o['total_R_net']:+.2f}")
        print(f"  expectancy    : {o['expectancy_R_net']:+.3f} R")
        print(f"  max DD        : {s['max_dd_pct']}%")
        print(f"  reasons       : {s['by_reason']}")
        if o.get('avg_MAE_R_winners') is not None:
            print(f"  avg MAE win   : {o['avg_MAE_R_winners']:+.2f} R")
        if o.get('avg_MFE_R_winners') is not None:
            print(f"  avg MFE win   : {o['avg_MFE_R_winners']:+.2f} R")

    _p("FULL",           report["full"])
    _p("IN-SAMPLE",      report["in_sample"])
    _p("OUT-OF-SAMPLE",  report["out_of_sample"])

    print("\n✓ backtest_report.json")
    print("✓ backtest_trades.csv")


if __name__ == "__main__":
    main()