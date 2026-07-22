from optionmaster.market.models import MarketFeatures, MarketSnapshot, OptionQuote, OptionSide, Regime, Signal
from optionmaster.strategy.profiles import BASELINE_PROFILE, StrategyProfile


def _candidate_score(
    quote: OptionQuote, features: MarketFeatures, profile: StrategyProfile = BASELINE_PROFILE
) -> float:
    delta_score = min(abs(quote.delta) / 0.55, 1.0)
    momentum_score = min(abs(features.momentum) / 2.0, 1.0)
    liquidity_score = max(0.0, 1.0 - quote.spread_fraction / profile.maximum_option_spread_fraction)
    oi_score = min(abs(quote.oi_change) / max(quote.oi, 1.0) * 10.0, 1.0)
    return round(0.35 * delta_score + 0.30 * momentum_score + 0.25 * liquidity_score + 0.10 * oi_score, 4)


def select_signal(
    snapshot: MarketSnapshot,
    features: MarketFeatures,
    regime: Regime,
    profile: StrategyProfile = BASELINE_PROFILE,
) -> Signal:
    if regime not in (Regime.BULLISH_TREND, Regime.BEARISH_TREND):
        return Signal(
            symbol=snapshot.symbol,
            timestamp=snapshot.timestamp,
            regime=regime,
            strategy_id=profile.id,
            reason="No directional regime with sufficient confirmation.",
        )

    target = OptionSide.CE if regime is Regime.BULLISH_TREND else OptionSide.PE
    candidates = [
        q for q in snapshot.option_quotes
        if (
            q.side is target
            and q.ltp > 0
            and profile.minimum_option_delta <= abs(q.delta) <= profile.maximum_option_delta
            and q.spread_fraction <= profile.maximum_option_spread_fraction
        )
    ]
    if not candidates:
        return Signal(
            symbol=snapshot.symbol,
            timestamp=snapshot.timestamp,
            regime=regime,
            strategy_id=profile.id,
            reason=f"No liquid {target} contract in the configured delta range.",
        )

    quote = max(candidates, key=lambda q: _candidate_score(q, features, profile))
    score = _candidate_score(quote, features, profile)
    return Signal(
        symbol=snapshot.symbol,
        timestamp=snapshot.timestamp,
        regime=regime,
        strategy_id=profile.id,
        side=quote.side,
        strike=quote.strike,
        score=score,
        reason=f"{regime}: selected {quote.side} by delta, liquidity, momentum, and OI change.",
    )
