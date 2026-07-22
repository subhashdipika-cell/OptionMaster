from optionmaster.market.models import MarketFeatures, Regime
from optionmaster.strategy.profiles import BASELINE_PROFILE, StrategyProfile


def detect_regime(features: MarketFeatures, profile: StrategyProfile = BASELINE_PROFILE) -> Regime:
    """Conservative first-pass regime detector; no trade is a valid outcome."""
    if features.liquid_quote_count < profile.minimum_liquid_quotes or features.india_vix >= profile.max_india_vix:
        return Regime.HIGH_VOLATILITY if features.india_vix >= profile.extreme_india_vix else Regime.NO_TRADE
    if abs(features.momentum) < 0.04 and abs(features.underlying_change_pct) < 0.08:
        return Regime.RANGE_BOUND
    if (
        features.momentum >= profile.minimum_momentum_pct
        and features.underlying_change_pct >= profile.minimum_underlying_change_pct
        and features.put_oi_change > features.call_oi_change
    ):
        return Regime.BULLISH_TREND
    if (
        features.momentum <= -profile.minimum_momentum_pct
        and features.underlying_change_pct <= -profile.minimum_underlying_change_pct
        and features.call_oi_change > features.put_oi_change
    ):
        return Regime.BEARISH_TREND
    return Regime.NO_TRADE
