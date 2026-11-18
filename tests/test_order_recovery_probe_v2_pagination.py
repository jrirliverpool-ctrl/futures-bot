
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "scripts"),
)

from order_recovery_probe import OrderRecoveryProbe


SYMBOL = "BTC-SWAP-USDT"
NOW = 1790000005000
CREATED = 1790000000000


def make_trade(trade_id, qty):
    return {
        "time": str(CREATED + 100),
        "id": str(trade_id),
        "orderId": "order-1",
        "symbol": SYMBOL,
        "price": "100",
        "qty": str(qty),
        "commissionAsset": "USDT",
        "commission": "0.01",
        "makerRebate": "0",
        "type": "MARKET",
        "isMaker": False,
        "side": "BUY",
        "positionSide": "LONG",
        "realizedPnl": "0",
        "ticketId": f"ticket-{trade_id}",
    }


def make_order():
    return {
        "orderId": "order-1",
        "clientOrderId": "client-1",
        "symbol": SYMBOL,
        "side": "BUY",
        "positionSide": "LONG",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "100",
        "status": "FILLED",
    }


def make_record():
    return {
        "client_order_id": "client-1",
        "status": "SUBMISSION_STARTED",
        "created_at_ms": CREATED,
        "exchange_order_id": None,
        "intent": {
            "client_order_id": "client-1",
            "symbol": SYMBOL,
            "side": "LONG",
            "intent": "ENTRY",
            "order_type": "MARKET",
            "qty": "1",
            "price": None,
            "leverage": 3,
            "signal_id": "signal-1",
            "strategy_hash": "sha256:" + "a" * 64,
        },
    }


class FakeStore:
    def __init__(self):
        self.write_calls = 0

    def recovery_required(self):
        return [make_record()]

    def create_intent(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("probe must remain read-only")

    def mark_submission_started(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("probe must remain read-only")

    def mark_submission_unknown(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("probe must remain read-only")

    def acknowledge(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("probe must remain read-only")

    def mark_reconciled(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("probe must remain read-only")


class FakeV2Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_order_status_v2(self, symbol, client_order_id):
        assert symbol == SYMBOL
        assert client_order_id == "client-1"
        return SimpleNamespace(
            status="FILLED",
            raw_response={"data": make_order()},
        )

    def get_user_trades_v2(
        self,
        symbol,
        start_time=None,
        end_time=None,
        limit=1000,
        from_id=None,
        to_id=None,
    ):
        self.calls.append({
            "symbol": symbol,
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
            "from_id": from_id,
        })

        if not self.pages:
            raise AssertionError("unexpected additional page request")

        page = self.pages.pop(0)

        if isinstance(page, Exception):
            raise page

        return page


def test_v2_recovery_uses_all_pages_before_fill_reconciliation():
    client = FakeV2Client([
        [
            make_trade(12, "0.4"),
            make_trade(11, "0.6"),
        ],
        [],
    ])
    store = FakeStore()

    result = OrderRecoveryProbe(
        client,
        store,
        trade_limit=2,
    ).run(now_ms=NOW)

    assert result["ok"] is True
    assert result["halt_trading"] is True
    assert result["read_only"] is True
    assert result["journal_mutated"] is False
    assert result["trading_state_mutated"] is False

    item = result["items"][0]
    assert item["can_be_marked_reconciled"] is True
    assert item["trades_api_version"] == "V2"
    assert item["reconciliation"]["ledger_filled_qty"] == pytest.approx(1.0)

    assert len(client.calls) == 2
    assert client.calls[0]["from_id"] is None
    assert client.calls[1]["from_id"] == 11

    assert all(call["start_time"] == CREATED for call in client.calls)
    assert all(call["end_time"] == NOW for call in client.calls)

    assert store.write_calls == 0


def test_v2_duplicate_or_incomplete_next_page_fails_closed():
    client = FakeV2Client([
        [
            make_trade(12, "0.4"),
            make_trade(11, "0.6"),
        ],
        [
            make_trade(11, "0.6"),
        ],
    ])

    result = OrderRecoveryProbe(
        client,
        FakeStore(),
        trade_limit=2,
    ).run(now_ms=NOW)

    assert result["ok"] is False
    assert result["halt_trading"] is True
    assert result["items"][0]["can_be_marked_reconciled"] is False
    assert result["items"][0]["reason"] == "read_only_probe_error"
    assert "pagination" in result["items"][0]["detail"].lower()
