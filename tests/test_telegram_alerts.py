from datetime import datetime, timezone

from optionmaster.alerts.telegram import TelegramNotifier
from optionmaster.market.models import OptionSide
from optionmaster.paper.models import PaperTrade, PaperTradeStatus


def _trade(status: PaperTradeStatus) -> PaperTrade:
    return PaperTrade(
        symbol="NIFTY",
        underlying_security_id=13,
        underlying_segment="IDX_I",
        underlying_instrument_type="INDEX",
        contract_security_id=42,
        lot_size=75,
        expiry="2026-07-21",
        side=OptionSide.CE,
        strike=24000,
        quantity=75,
        status=status,
        opened_at=datetime.now(timezone.utc),
        entry_price=100.0,
        current_price=100.0,
        stop_loss=90.0,
        target=115.0,
        premium_paid=7500.0,
        maximum_risk=900.0,
        rationale="test",
    )


def test_unconfigured_notifier_sends_nothing():
    notifier = TelegramNotifier(None, None)
    assert not notifier.configured
    assert notifier.send_text("hello", wait=True) is False


def test_notify_helpers_never_raise_when_unconfigured():
    notifier = TelegramNotifier("", "")
    notifier.notify_open(_trade(PaperTradeStatus.OPEN))
    closed = _trade(PaperTradeStatus.TARGET_HIT)
    closed.exit_price = 115.0
    closed.realized_pnl = 1000.0
    closed.closed_at = datetime.now(timezone.utc)
    notifier.notify_close(closed)
