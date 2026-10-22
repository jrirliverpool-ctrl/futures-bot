"""
audit_trades.py — Detailed audit of Flex_D15 IS trades.
Fixes: Requests 4100 candles to ensure 4000 closed are available.
"""
import os
import sys
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backtest
import strategy_flexible
from sweep_oos import fetch_klines_vision
from symbol_specs import get_symbol_specs

def main():
    # ✅ Request 4100 candles (one may be unclosed)
    klines = fetch_klines_vision(4100)
    
    # ✅ Use the last 4000 closed candles
    if len(klines) < 4000:
        raise SystemExit(f"❌ Need 4000 closed candles, got {len(klines)}")
    klines = klines[-4000:]
    
    # ✅ IS period: 1250 input candles (250 warmup + 1000 eval)
    klines_is = klines[:1250]
    
    specs = get_symbol_specs("BTC-SWAP-USDT")
    specs["contract_multiplier"] = 0.001
    
    def mock_indicator_params():
         from strategy_config import get_indicator_params
         params = get_indicator_params()
         params["donchian_p"] = 15
         return params

    with patch("backtest.get_indicator_params", side_effect=mock_indicator_params), \
         patch("backtest.evaluate_signal", side_effect=strategy_flexible.evaluate_flexible_signal):
         result = backtest.run_backtest(klines_is, specs, initial_equity=1000.0, symbol="BTC-SWAP-USDT", start_index=250)
         
    events = result["events"]
    final_state = result["final_state"]
    
    if final_state.get("position") is not None:
        print("⚠️  WARNING: An open position exists at the end of the IS period.")
    
    trades = []
    current_entry = None
    tp1_info = None
    
    for e in events:
        if e["type"] == "ENTRY":
            current_entry = e
            tp1_info = None
        elif e["type"] == "TP1_HIT" and current_entry:
            tp1_info = e
        elif e["type"] == "EXIT" and current_entry:
            trades.append({
                "entry_time": datetime.fromtimestamp(current_entry["timestamp"]/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                "side": current_entry["side"],
                "entry_price": current_entry["price"],
                "exit_time": datetime.fromtimestamp(e["timestamp"]/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                "exit_price": e["price"],
                "reason": e["reason"],
                "gross_pnl": e.get("pnl_usd", 0.0),
                "net_pnl": e.get("trade_net_pnl_usd", 0.0),
                "commission": e.get("total_commission_usd", 0.0),
                "tp1_hit": tp1_info is not None
            })
            current_entry = None
            
    print("=" * 170)
    print(f"AUDIT: Flex_D15 on IS (Total Trades: {len(trades)})")
    print("=" * 170)
    print(f"{'Entry Time':<24} | {'Side':<5} | {'Entry':<10} | {'Exit Time':<24} | {'Exit':<10} | {'Reason':<6} | {'Gross':<9} | {'Commission':<11} | {'Net':<9} | {'TP1?'}")
    print("-" * 170)
    
    for t in trades:
        print(
            f"{t['entry_time']:<24} | {t['side']:<5} | "
            f"{t['entry_price']:<10.2f} | "
            f"{t['exit_time']:<24} | {t['exit_price']:<10.2f} | "
            f"{t['reason']:<6} | {t['gross_pnl']:<9.2f} | "
            f"{t['commission']:<11.2f} | {t['net_pnl']:<9.2f} | {t['tp1_hit']}"
        )

if __name__ == "__main__":
    main()