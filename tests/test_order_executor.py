import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from order_executor import OrderRequest, evaluate, DEFAULT_CONFIG

BASE_SPECS = {"contract_multiplier": 0.001, "min_qty": 0.1, "max_qty": 100.0, "qty_step": 0.1, "min_notional": 0.0, "tick_size": 0.1, "max_leverage": 10}

def _req(**kwargs):
    d = {"symbol": "BTC-SWAP-USDT", "side": "LONG", "order_type": "MARKET", "qty": 1.0, "price": None, "leverage": 3, "intent": "ENTRY"}
    d.update(kwargs)
    return OrderRequest(**d)

def test_entry_valid():
    assert evaluate(_req(), BASE_SPECS, {}, DEFAULT_CONFIG, ref_price=100.0).accepted is True

def test_specs_nan_rejected():
    specs = dict(BASE_SPECS); specs["min_qty"] = float("nan")
    assert evaluate(_req(), specs, {}, DEFAULT_CONFIG, ref_price=100.0).accepted is False

def test_specs_bool_rejected():
    specs = dict(BASE_SPECS); specs["qty_step"] = True
    assert evaluate(_req(), specs, {}, DEFAULT_CONFIG, ref_price=100.0).accepted is False

def test_exit_wrong_side_rejected():
    state = {"position": {"side": "LONG", "remaining_size": 1.0}}
    assert evaluate(_req(intent="EXIT", side="SHORT"), BASE_SPECS, state, DEFAULT_CONFIG, ref_price=100.0).accepted is False

def test_exit_position_side_missing_rejected():
    state = {"position": {"remaining_size": 1.0}}  # side missing
    assert evaluate(_req(intent="EXIT"), BASE_SPECS, state, DEFAULT_CONFIG, ref_price=100.0).accepted is False

def test_halt_blocks_entry():
    assert evaluate(_req(), BASE_SPECS, {"halt_trading": True}, DEFAULT_CONFIG, ref_price=100.0).accepted is False

def test_pending_order_id_blocks_entry():
    state = {"pending_order_id": "apex_123"}
    assert evaluate(_req(), BASE_SPECS, state, DEFAULT_CONFIG, ref_price=100.0).accepted is False

def test_tp1_hit_valid():
    state = {"position": {"side": "LONG", "remaining_size": 1.0}}
    assert evaluate(_req(intent="TP1_HIT", side="LONG", qty=0.5), BASE_SPECS, state, DEFAULT_CONFIG, ref_price=100.0).accepted is True

def test_hedge_mode_allows_opposite_side_when_portfolio_limit_allows():
    state = {"positions": {"LONG": {"side": "LONG", "remaining_size": 1.0}}}
    config = {**DEFAULT_CONFIG, "max_concurrent_positions": 2}
    assert evaluate(_req(side="SHORT"), BASE_SPECS, state, config, ref_price=100.0).accepted is True

def test_hedge_mode_still_blocks_same_side_duplicate():
    state = {"positions": {"LONG": {"side": "LONG", "remaining_size": 1.0}}}
    config = {**DEFAULT_CONFIG, "max_concurrent_positions": 2}
    decision = evaluate(_req(side="LONG"), BASE_SPECS, state, config, ref_price=100.0)
    assert decision.accepted is False
    assert decision.gate == "duplicate"

def test_hedge_mode_exit_selects_only_requested_side():
    state = {"positions": {
        "LONG": {"side": "LONG", "remaining_size": 1.0},
        "SHORT": {"side": "SHORT", "remaining_size": 2.0},
    }}
    long_exit = evaluate(_req(intent="EXIT", side="LONG"), BASE_SPECS, state, ref_price=100.0)
    short_exit = evaluate(_req(intent="EXIT", side="SHORT", qty=2.1), BASE_SPECS, state, ref_price=100.0)
    assert long_exit.accepted is True
    assert short_exit.accepted is False
    assert short_exit.gate == "exit_qty"

def test_hedge_mode_pending_entry_reserves_only_its_side():
    state = {"pending_entries": {"LONG": {"side": "LONG"}}}
    config = {**DEFAULT_CONFIG, "max_concurrent_positions": 2}
    assert evaluate(_req(side="SHORT"), BASE_SPECS, state, config, ref_price=100.0).accepted is True

def test_malformed_hedge_state_fails_closed():
    state = {"positions": {"LONG": {"side": "SHORT", "remaining_size": 1.0}}}
    decision = evaluate(_req(side="SHORT"), BASE_SPECS, state, {**DEFAULT_CONFIG, "max_concurrent_positions": 2}, ref_price=100.0)
    assert decision.accepted is False
