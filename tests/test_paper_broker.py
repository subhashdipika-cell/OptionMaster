from datetime import datetime, timezone

import pytest

from optionmaster.market.models import MarketSnapshot, OptionQuote, OptionSide, Regime, Signal
from optionmaster.paper.broker import PaperBroker, PaperTradeRejected
from optionmaster.paper.models import CreatePaperTradeRequest, PaperTradeStatus


def _snapshot(bid: float = 99, ask: float = 100) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="NIFTY",
        timestamp=datetime.now(timezone.utc),
        underlying=25000,
        option_quotes=[
            OptionQuote(security_id=44900, strike=25000, side=OptionSide.CE, ltp=99.5, bid=bid, ask=ask, delta=0.5)
        ],
    )


def _signal() -> Signal:
    return Signal(
        symbol="NIFTY",
        timestamp=datetime.now(timezone.utc),
        regime=Regime.BULLISH_TREND,
        side=OptionSide.CE,
        strike=25000,
        score=0.8,
        reason="test",
    )


def _request() -> CreatePaperTradeRequest:
    return CreatePaperTradeRequest(
        symbol="NIFTY", security_id=13, segment="IDX_I", expiry="2026-07-21", quantity=25, capital=100000
    )


def test_paper_trade_uses_ask_to_enter_and_bid_to_exit():
    broker = PaperBroker()
    trade = broker.open_from_signal(
        request=_request(), signal=_signal(), snapshot=_snapshot(), quantity=25, lot_size=25, contract_security_id=44900
    )

    assert trade.entry_price == 100
    assert trade.premium_paid == 2500
    marked = broker.mark_to_market(trade.id, _snapshot(bid=130, ask=131))
    assert marked.status is PaperTradeStatus.TARGET_HIT
    assert marked.gross_pnl == 750
    assert 0 < marked.realized_pnl < marked.gross_pnl
    assert marked.charges is not None and marked.charges.total > 0


def test_paper_trade_rejects_capital_overallocation():
    broker = PaperBroker()
    request = _request()
    request.capital = 5000
    with pytest.raises(PaperTradeRejected, match="capital allocation"):
        broker.open_from_signal(
            request=request, signal=_signal(), snapshot=_snapshot(), quantity=25, lot_size=25, contract_security_id=44900
        )
