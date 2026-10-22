"""
audit_trades.py — Detailed audit of Flex_D15 IS trades.
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
    klines = fetch_klines_vision(4000)
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
                "entry_time": datetime.fromtimestamp(current_entry["timestamp"]/1000, tz=timezone.utc).strftime('%m-%d %H:%M'),
                "side": current_entry["side"],
                "entry_price": current_entry["price"],
                "exit_time": datetime.fromtimestamp(e["timestamp"]/1000, tz=timezone.utc).strftime('%m-%d %H:%M'),
                "exit_price": e["price"],
                "reason": e["reason"],
                "gross_pnl": e.get("pnl_usd", 0.0),
                "net_pnl": e.get("trade_net_pnl_usd", 0.0),
                "commission": e.get("total_commission_usd", 0.0),
                "tp1_hit": tp1_info is not None
            })
            current_entry = None
            
    print("=" * 120)
    print(f"AUDIT: Flex_D15 on IS (Total Trades: {len(trades)})")
    print("=" * 120)
    print(f"{'Entry Time':<12} | {'Side':<5} | {'Entry':<8} | {'Exit Time':<12} | {'Exit':<8} | {'Reason':<8} | {'Gross':<7} | {'Net':<7} | {'TP1?'}")
    print("-" * 120)
    for t in trades:
        print(f"{t['entry_time']:<12} | {t['side']:<5} | {t['entry_price']:<8.2f} | {t['exit_time']:<12} | {t['exit_price']:<8.2f} | {t['reason']:<8} | {t['gross_pnl']:<7.2f} | {t['net_pnl']:<7.2f} | {t['tp1_hit']}")

if __name__ == "__main__":
    main()