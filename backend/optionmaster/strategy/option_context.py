"""Shared index-context and selected-option confirmation rules.

All OptionMaster strategies use the index only for direction and structure.
The tradable contract is then selected from the option chain and confirmed
against its own observed premium path.  The archive contains point-in-time
quotes rather than option OHLC candles, so premium support/resistance is
derived only from the selected contract's prior LTP observations.
"""
from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
from statistics import fmean


@dataclass(frozen=True, slots=True)
class OptionContextSetup:
    side: str
    strike: float
    entry_price: float
    stop_loss: float
    target: float
    index_support: float | None
    index_resistance: float | None
    index_support_trendline: float | None
    index_resistance_trendline: float | None
    index_trend: str
    premium_support: float
    premium_resistance: float
    reason: str


_CANDLE_TIMES: dict[int, tuple[object, list]] = {}


def _closed_candles(candles, timestamp):
    if not candles:
        return []
    key = id(candles)
    cached = _CANDLE_TIMES.get(key)
    if cached is None or cached[0] is not candles:
        cached = (candles, [item.close_time for item in candles])
        _CANDLE_TIMES[key] = cached
    index = bisect_right(cached[1], timestamp)
    return candles[max(0, index - 30):index]


def _ema(values: list[float], period: int = 20) -> float:
    if not values:
        return 0.0
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _atr(candles, period: int = 14) -> float:
    if not candles:
        return 0.0
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        previous = candles[index - 1].close if index else candle.close
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
    return max(fmean(ranges[-period:]), 0.01)


def _index_levels(candles) -> tuple[float | None, float | None]:
    if not candles:
        return None, None
    recent = candles[-20:]
    return min(item.low for item in recent), max(item.high for item in recent)


def _trendline_levels(candles) -> tuple[float | None, float | None]:
    """Project simple last-two swing trendlines without using future candles."""
    if len(candles) < 7:
        return None, None
    lows = [(i, candles[i].low) for i in range(1, len(candles) - 1) if candles[i].low <= candles[i - 1].low and candles[i].low <= candles[i + 1].low]
    highs = [(i, candles[i].high) for i in range(1, len(candles) - 1) if candles[i].high >= candles[i - 1].high and candles[i].high >= candles[i + 1].high]
    def project(points):
        if len(points) < 2:
            return None
        (a_i, a_p), (b_i, b_p) = points[-2:]
        return b_p + (b_p - a_p) / max(1, b_i - a_i) * (len(candles) - 1 - b_i)
    return project(lows), project(highs)


def _index_direction(candles, side: str) -> tuple[bool, str, float | None, float | None]:
    if len(candles) < 20:
        return False, "INSUFFICIENT_INDEX_DATA", *_index_levels(candles)
    closes = [item.close for item in candles]
    current = closes[-1]
    fast = _ema(closes[-9:], 9)
    slow = _ema(closes[-20:], 20)
    slope = slow - _ema(closes[-25:-5], 20) if len(closes) >= 25 else 0.0
    support, resistance = _index_levels(candles)
    bullish = current >= fast >= slow and slope >= 0
    bearish = current <= fast <= slow and slope <= 0
    allowed = bullish if side == "CE" else bearish
    return allowed, "TREND_UP" if bullish else "TREND_DOWN" if bearish else "CHOP", support, resistance


def _candidate_score(quote, *, min_delta: float, max_delta: float, max_spread_pct: float, spot: float) -> float:
    delta = abs(float(quote.delta))
    ideal = (min_delta + max_delta) / 2
    delta_score = max(0.0, 1.0 - abs(delta - ideal) / max(ideal, 0.01))
    spread_score = max(0.0, 1.0 - float(quote.spread_pct or 999) / max(max_spread_pct, 0.01))
    oi_change = abs(float(quote.oi_change)) / max(float(quote.oi), 1.0)
    volume_score = min(float(quote.volume) / max(float(quote.oi), 1.0) * 100.0, 1.0)
    distance_score = max(0.0, 1.0 - abs(float(quote.strike) - spot) / max(spot * 0.02, 1.0))
    return 0.35 * delta_score + 0.25 * spread_score + 0.20 * min(oi_change * 10, 1.0) + 0.10 * volume_score + 0.10 * distance_score


def select_and_confirm(
    *, snapshots, snapshot, candles, side: str, min_premium: float, max_premium: float,
    max_spread_pct: float, min_delta: float = 0.25, max_delta: float = 0.65,
    lookback: int = 12, target_r: float = 1.8, require_confirmation: bool = True,
) -> OptionContextSetup | None:
    """Return a contract and premium-based plan, or None when the full gate fails."""
    candles = _closed_candles(candles, snapshot.timestamp)
    index_ok, trend, support, resistance = _index_direction(candles, side)
    trendline_support, trendline_resistance = _trendline_levels(candles)
    if not index_ok:
        return None
    candidates = [
        quote for quote in snapshot.quotes.values()
        if quote.side == side and quote.bid > 0 and quote.ask > 0 and quote.ltp >= min_premium
        and quote.ask <= max_premium and quote.spread_pct is not None
        and quote.spread_pct <= max_spread_pct and min_delta <= abs(quote.delta) <= max_delta
        and quote.oi > 0 and quote.volume > 0
    ]
    if not candidates:
        return None
    quote = max(candidates, key=lambda item: _candidate_score(item, min_delta=min_delta, max_delta=max_delta, max_spread_pct=max_spread_pct, spot=snapshot.spot))
    prior = []
    for item in reversed(snapshots):
        if item.timestamp >= snapshot.timestamp:
            continue
        prior.append(item.quote(quote.strike, side))
        if len(prior) >= lookback:
            break
    prior.reverse()
    prices = [float(item.ltp) for item in prior[-lookback:] if item is not None and item.ltp > 0]
    if len(prices) < 3:
        return None
    premium_support = min(prices)
    premium_resistance = max(prices)
    premium_atr = max((max(prices[i], prices[i - 1]) - min(prices[i], prices[i - 1]) for i in range(1, len(prices))), default=quote.ltp * 0.05)
    ema = _ema(prices, min(9, len(prices)))
    vwap = fmean(prices)
    trigger = premium_resistance + premium_atr * 0.05
    if require_confirmation and not (quote.ltp >= trigger and quote.ltp >= ema and quote.ltp >= vwap):
        return None
    risk = max(0.05, float(quote.ltp) - max(0.05, premium_support - premium_atr * 0.20))
    stop = round(max(0.05, premium_support - premium_atr * 0.20), 2)
    target = round(float(quote.ask) + risk * target_r, 2)
    return OptionContextSetup(
        side=side, strike=quote.strike, entry_price=float(quote.ask), stop_loss=stop, target=target,
        index_support=support, index_resistance=resistance, index_trend=trend,
        premium_support=round(premium_support, 2), premium_resistance=round(premium_resistance, 2),
        reason=f"{trend}: index S/R {support}/{resistance}, trendline S/R {trendline_support}/{trendline_resistance}; selected {side} {quote.strike}; premium S/R {premium_support:.2f}/{premium_resistance:.2f} confirmed.",
        index_support_trendline=trendline_support, index_resistance_trendline=trendline_resistance,
    )
