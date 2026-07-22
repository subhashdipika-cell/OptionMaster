from datetime import datetime, timedelta, timezone

from optionmaster.context.engine import ContextEngine
from optionmaster.context.models import ContextEvaluationRequest, PriceBar, ShadowAction
from optionmaster.market.models import MarketSnapshot, OptionQuote, OptionSide, Regime, Signal


def _bars() -> list[PriceBar]:
    start = datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)
    return [
        PriceBar(
            timestamp=start + timedelta(minutes=5 * index),
            open=24880 + (index * 2),
            high=24883 + (index * 2),
            low=24878 + (index * 2),
            close=24881 + (index * 2),
            volume=1000,
        )
        for index in range(60)
    ]


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="NIFTY",
        timestamp=datetime.now(timezone.utc),
        underlying=25000,
        underlying_change_pct=0.4,
        underlying_momentum=0.1,
        option_quotes=[
            OptionQuote(strike=25000, side=OptionSide.CE, ltp=100, bid=99.9, ask=100.1, delta=0.5, oi=2000, previous_oi=1900, volume=1000),
            OptionQuote(strike=25100, side=OptionSide.CE, ltp=60, bid=59.9, ask=60.1, delta=0.35, oi=10000, previous_oi=10000, volume=100),
            OptionQuote(strike=24900, side=OptionSide.PE, ltp=55, bid=54.9, ask=55.1, delta=-0.4, oi=12000, previous_oi=11500, volume=600),
        ],
    )


def _signal() -> Signal:
    return Signal(
        symbol="NIFTY",
        timestamp=datetime.now(timezone.utc),
        regime=Regime.BULLISH_TREND,
        strategy_id="baseline-v1",
        side=OptionSide.CE,
        strike=25000,
        score=0.8,
        reason="test",
    )


def test_context_engine_caps_target_before_resistance_and_allows_fresh_setup():
    result = ContextEngine().evaluate(
        ContextEvaluationRequest(
            snapshot=_snapshot(), signal=_signal(), bars=_bars(), signal_origin_price=24996, signal_age_candles=1
        )
    )

    assert result.action is ShadowAction.WOULD_ALLOW
    assert result.nearest_opposing_level is not None
    assert result.recommended_underlying_target is not None
    assert result.recommended_underlying_target < result.nearest_opposing_level.price
    assert result.risk_reward_to_structure is not None and result.risk_reward_to_structure >= 1.5


def test_context_engine_marks_extended_signal_for_pullback_in_shadow_mode():
    result = ContextEngine().evaluate(
        ContextEvaluationRequest(
            snapshot=_snapshot(), signal=_signal(), bars=_bars(), signal_origin_price=24900, signal_age_candles=4
        )
    )

    assert result.action is ShadowAction.WOULD_SKIP
    assert result.freshness.entry_preference == "WAIT_FOR_PULLBACK"
    assert any("Would skip" in reason for reason in result.reasons)
