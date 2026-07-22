from statistics import fmean

from optionmaster.market.models import MarketFeatures, MarketSnapshot, OptionSide


def build_features(snapshot: MarketSnapshot) -> MarketFeatures:
    calls = [q for q in snapshot.option_quotes if q.side is OptionSide.CE]
    puts = [q for q in snapshot.option_quotes if q.side is OptionSide.PE]
    call_oi_change = sum(q.oi_change for q in calls)
    put_oi_change = sum(q.oi_change for q in puts)
    total_call_oi = sum(q.oi for q in calls)
    pcr = sum(q.oi for q in puts) / total_call_oi if total_call_oi else 0.0
    ivs = [q.iv for q in snapshot.option_quotes if q.iv > 0]
    liquid = [q for q in snapshot.option_quotes if q.ltp > 0 and q.spread_fraction <= 0.03]

    return MarketFeatures(
        symbol=snapshot.symbol,
        timestamp=snapshot.timestamp,
        underlying_change_pct=snapshot.underlying_change_pct,
        momentum=snapshot.underlying_momentum,
        india_vix=snapshot.india_vix,
        call_oi_change=call_oi_change,
        put_oi_change=put_oi_change,
        put_call_oi_ratio=pcr,
        average_iv=fmean(ivs) if ivs else 0.0,
        liquid_quote_count=len(liquid),
    )
