from datetime import datetime, timedelta, timezone

from optionmaster.journal.store import TradeJournal
from optionmaster.market.models import OptionSide, Regime
from optionmaster.paper.broker import PaperBroker
from optionmaster.paper.models import PaperTrade, PaperTradeStatus


def _trade(*, expiry: str, status: PaperTradeStatus = PaperTradeStatus.OPEN) -> PaperTrade:
    now = datetime.now(timezone.utc)
    return PaperTrade(
        symbol="NIFTY", strategy_id="baseline-v1", regime=Regime.BEARISH_TREND,
        underlying_security_id=13, underlying_segment="IDX_I", underlying_instrument_type="INDEX",
        contract_security_id=41000, lot_size=65, expiry=expiry, side=OptionSide.PE,
        strike=24500, quantity=65, status=status, opened_at=now, entry_price=100,
        current_price=100, stop_loss=80, target=130, premium_paid=6500,
        maximum_risk=1350, rationale="BEARISH_TREND: test record.",
    )


def test_expired_open_trade_is_quarantined_and_excluded_from_performance(tmp_path):
    journal = TradeJournal(tmp_path / "optionmaster.db")
    stale = _trade(expiry="2020-01-01")
    journal.record_paper_trade(stale)

    broker = PaperBroker(journal=journal)
    restored = journal.list_paper_trades()[0]

    assert restored.status is PaperTradeStatus.UNRECONCILED
    assert broker.list_trades() == []
    assert journal.performance("paper").closed_trades == 0


def test_valid_open_trade_is_restored_after_restart(tmp_path):
    journal = TradeJournal(tmp_path / "optionmaster.db")
    future = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
    live = _trade(expiry=future)
    journal.record_paper_trade(live)

    broker = PaperBroker(journal=journal)

    assert [trade.id for trade in broker.list_trades()] == [live.id]
