from datetime import date, datetime, timedelta, timezone

from optionmaster.market.models import OptionSide
from optionmaster.scalping.engine import ScalpingEngine
from optionmaster.scalping.models import MarketTick, ScalpingAction, ScalpingSessionRequest, TickKind


def test_delta_sync_cvd_and_tight_spread_create_market_eligible_signal():
    engine = ScalpingEngine(
        ScalpingSessionRequest(
            symbol="NIFTY", spot_security_id=13, option_security_id=44900,
            option_side=OptionSide.CE, expiry=date.today(), lot_size=75,
        )
    )
    start = datetime.now(timezone.utc)
    for index in range(7):
        time = start + timedelta(milliseconds=index * 140)
        engine.ingest(MarketTick(security_id=13, kind=TickKind.SPOT, timestamp=time, ltp=25000, bid=24999.9, ask=25000.1))
        engine.ingest(MarketTick(security_id=44900, kind=TickKind.OPTION, timestamp=time, ltp=100, bid=99.9, ask=100.1, last_quantity=10, cumulative_volume=(index + 1) * 10))
    result = engine.ingest(MarketTick(
        security_id=44900, kind=TickKind.OPTION, timestamp=start + timedelta(seconds=1.05),
        ltp=100.5, bid=100.4, ask=100.5, last_quantity=50, cumulative_volume=120,
    ))
    assert result.action is ScalpingAction.MARKET_ELIGIBLE
