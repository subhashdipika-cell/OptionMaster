from datetime import datetime, timezone

from optionmaster.journal.store import TradeJournal
from optionmaster.learning.service import LearningService, StrategyProfileRegistry
from optionmaster.market.models import OptionSide
from optionmaster.paper.models import PaperTrade, PaperTradeStatus


def _closed_trade(index: int, net_pnl: float) -> PaperTrade:
    now = datetime.now(timezone.utc)
    return PaperTrade(
        id=f"trade-{index}",
        strategy_id="tight-momentum-v1",
        symbol="NIFTY",
        underlying_security_id=13,
        underlying_segment="IDX_I",
        underlying_instrument_type="INDEX",
        contract_security_id=44900,
        lot_size=75,
        expiry="2026-07-21",
        side=OptionSide.CE,
        strike=25000,
        quantity=75,
        status=PaperTradeStatus.CLOSED,
        opened_at=now,
        closed_at=now,
        entry_price=100,
        current_price=101,
        exit_price=101,
        stop_loss=80,
        target=130,
        premium_paid=7500,
        maximum_risk=1600,
        gross_pnl=net_pnl,
        realized_pnl=net_pnl,
        rationale="test",
    )


def test_candidate_profile_needs_and_then_can_clear_forward_paper_evidence(tmp_path):
    journal = TradeJournal(tmp_path / "optionmaster.db")
    profiles = StrategyProfileRegistry(journal)
    learning = LearningService(journal=journal, profiles=profiles)

    assert not learning.evaluate("tight-momentum-v1").eligible_for_paper_promotion

    for index in range(30):
        journal.record_paper_trade(_closed_trade(index, 100 if index < 25 else -50))

    evaluation = learning.evaluate("tight-momentum-v1")
    assert evaluation.eligible_for_paper_promotion
    assert learning.activate_if_eligible("tight-momentum-v1").id == "tight-momentum-v1"
    assert profiles.active().id == "tight-momentum-v1"
