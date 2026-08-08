from optionmaster.market.models import MarketFeatures, MarketSnapshot, OptionQuote, OptionSide, Regime, Signal
from optionmaster.strategy.profiles import BASELINE_PROFILE, StrategyProfile

_PREMIUM_HISTORY: dict[tuple[str, float, OptionSide], list[float]] = {}
_HISTORY_DAY: dict[str, object] = {}


def _premium_plan(snapshot: MarketSnapshot, quote: OptionQuote, *, target_r: float = 1.8):
    day = snapshot.timestamp.date()
    if _HISTORY_DAY.get(snapshot.symbol) != day:
        for key in [item for item in _PREMIUM_HISTORY if item[0] == snapshot.symbol]:
            _PREMIUM_HISTORY.pop(key, None)
        _HISTORY_DAY[snapshot.symbol] = day
    key = (snapshot.symbol, quote.strike, quote.side)
    prior = _PREMIUM_HISTORY.setdefault(key, [])
    before = prior[-12:]
    prior.append(float(quote.ltp))
    if len(before) < 3 or not all(value > 0 for value in before):
        return None
    support, resistance = min(before), max(before)
    average = sum(before) / len(before)
    move = max((max(before[i], before[i - 1]) - min(before[i], before[i - 1]) for i in range(1, len(before))), default=quote.ltp * 0.05)
    trigger = resistance + move * 0.05
    if quote.ltp < trigger or quote.ltp < average:
        return None
    stop = round(max(0.05, support - move * 0.20), 2)
    risk = max(0.05, quote.ltp - stop)
    return support, resistance, stop, round(quote.ask + risk * target_r, 2)


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
    plan = _premium_plan(snapshot, quote)
    if plan is None:
        return Signal(
            symbol=snapshot.symbol, timestamp=snapshot.timestamp, regime=regime,
            strategy_id=profile.id, reason=f"{regime}: selected option is awaiting its own premium-chart breakout confirmation.",
        )
    premium_support, premium_resistance, stop, target = plan
    return Signal(
        symbol=snapshot.symbol,
        timestamp=snapshot.timestamp,
        regime=regime,
        strategy_id=profile.id,
        side=quote.side,
        strike=quote.strike,
        score=score,
        reason=f"{regime}: selected {quote.side} by delta, liquidity, momentum, OI change, and premium-chart breakout.",
        option_support=premium_support, option_resistance=premium_resistance,
        entry_price=quote.ask, stop_loss_price=stop, target_price=target,
    )
