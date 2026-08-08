from datetime import date, datetime, timedelta, timezone

from optionmaster.market.models import OptionSide
from optionmaster.scalping.breakout_retest import BreakoutRetestScalpingEngine
from optionmaster.scalping.models import MarketTick, ScalpingAction, ScalpingSessionRequest, TickKind


IST = timezone(timedelta(hours=5, minutes=30))


def test_breakout_retest_checks_option_every_fifteen_seconds_and_marks_paper_exit():
    engine = BreakoutRetestScalpingEngine(ScalpingSessionRequest(
        symbol="NIFTY", spot_security_id=13, option_security_id=44900, option_side=OptionSide.CE,
        expiry=date.today(), strategy="BREAKOUT_RETEST_3_BAR",
    ))
    start = datetime(2026, 8, 7, 10, 0, tzinfo=IST)

    # Twelve flat bars establish a one-hour range at 100.
    for index in range(12):
        engine.ingest(MarketTick(
            security_id=13, kind=TickKind.SPOT,
            timestamp=start + timedelta(minutes=index * 5), ltp=100,
        ))
    # Breakout bar closes at 101; next bar retests 100 and closes green.
    engine.ingest(MarketTick(security_id=13, kind=TickKind.SPOT, timestamp=start + timedelta(minutes=60), ltp=101))
    engine.ingest(MarketTick(security_id=13, kind=TickKind.SPOT, timestamp=start + timedelta(minutes=65), ltp=100))
    engine.ingest(MarketTick(security_id=13, kind=TickKind.SPOT, timestamp=start + timedelta(minutes=69, seconds=45), ltp=101))
    engine.ingest(MarketTick(security_id=13, kind=TickKind.SPOT, timestamp=start + timedelta(minutes=70), ltp=101))

    entry_time = start + timedelta(minutes=70)
    baseline = engine.ingest(MarketTick(
        security_id=44900, kind=TickKind.OPTION, timestamp=entry_time,
        ltp=100, bid=99.9, ask=100.1,
    ))
    assert baseline.action is ScalpingAction.SKIP
    entry = engine.ingest(MarketTick(
        security_id=44900, kind=TickKind.OPTION, timestamp=entry_time + timedelta(seconds=15),
        ltp=100.4, bid=100.3, ask=100.5,
    ))
    assert entry.action is ScalpingAction.PAPER_ENTRY
    assert entry.paper_position_open
    waiting = engine.ingest(MarketTick(
        security_id=44900, kind=TickKind.OPTION, timestamp=entry_time + timedelta(seconds=25),
        ltp=105, bid=104.9, ask=105.1,
    ))
    assert waiting.action is ScalpingAction.SKIP
    closed = engine.ingest(MarketTick(
        security_id=44900, kind=TickKind.OPTION, timestamp=entry_time + timedelta(seconds=31),
        ltp=115.5, bid=115.2, ask=115.4,
    ))
    assert closed.action is ScalpingAction.PAPER_TARGET_EXIT
