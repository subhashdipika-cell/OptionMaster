"""Live, paper-trial evaluator for the fixed ORB/VWAP research candidate.

It shares the replay definition: M1 bars build session VWAP and Wilder
DMI/ADX, while complete five-minute bars define the opening range and the
breakout. It only returns a setup; it has no Dhan order functionality.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from optionmaster.backtest.orb_vwap import OrbVwapParams, _directional_movement, _session_vwap
from optionmaster.backtest.pullback import _resample
from optionmaster.backtest.spot import IST, SpotCandle
from optionmaster.context.models import PriceBar
from optionmaster.market.models import OptionSide


@dataclass(frozen=True, slots=True)
class OrbVwapSetup:
    side: OptionSide
    opening_high: float
    opening_low: float
    vwap: float
    adx: float
    signal_bar_closed_at: datetime


class PremiumMomentumTracker:
    """Keeps only the prior observed option LTP for paper-trial confirmation."""

    def __init__(self) -> None:
        self._prices: dict[tuple[float, OptionSide], float] = {}

    def observe_and_change(self, snapshot, *, strike: float, side: OptionSide) -> float | None:
        quote = next((item for item in snapshot.option_quotes if item.strike == strike and item.side is side), None)
        prior = self._prices.get((strike, side))
        self.observe(snapshot)
        if quote is None or quote.ltp <= 0 or prior is None or prior <= 0:
            return None
        return ((quote.ltp / prior) - 1) * 100

    def observe(self, snapshot) -> None:
        for item in snapshot.option_quotes:
            if item.ltp > 0:
                self._prices[(item.strike, item.side)] = item.ltp


def evaluate_m1_bars(
    bars: list[PriceBar], *, params: OrbVwapParams | None = None, now: datetime | None = None
) -> OrbVwapSetup | None:
    """Return the latest closed ORB setup from Dhan M1 bars, without lookahead."""
    params = params or OrbVwapParams()
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    candles = [
        SpotCandle(
            timestamp=bar.timestamp.astimezone(IST), open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume,
        )
        for bar in bars
        if bar.timestamp.astimezone(IST).date() == now_ist.date()
        and bar.timestamp.astimezone(IST) + timedelta(minutes=1) <= now_ist
    ]
    candles.sort(key=lambda item: item.timestamp)
    if len(candles) < params.adx_period * 2:
        return None
    five_minute = _resample(candles, params.bar_minutes)
    opening_bars = params.opening_range_minutes // params.bar_minutes
    if len(five_minute) <= opening_bars:
        return None
    bar = five_minute[-1]
    if bar.close_time > now_ist or not _within(bar.close_time, params.entry_start, params.entry_end):
        return None
    opening_high = max(item.high for item in five_minute[:opening_bars])
    opening_low = min(item.low for item in five_minute[:opening_bars])
    opening_volume = sum(item.volume for item in candles[:params.opening_range_minutes])
    if opening_volume <= 0:
        return None
    current_vwap = _session_vwap(candles)[-1]
    plus_di, minus_di, adx = _directional_movement(candles, params.adx_period)
    if current_vwap is None or plus_di[-1] is None or minus_di[-1] is None or adx[-1] is None:
        return None
    bar_volume = sum(item.volume for item in candles if bar.timestamp <= item.timestamp < bar.close_time)
    if bar_volume < (opening_volume / opening_bars) * params.volume_multiple:
        return None
    if bar.close > opening_high and bar.close > current_vwap and plus_di[-1] > minus_di[-1] and adx[-1] >= params.adx_minimum:
        return OrbVwapSetup(OptionSide.CE, opening_high, opening_low, current_vwap, adx[-1], bar.close_time)
    if bar.close < opening_low and bar.close < current_vwap and minus_di[-1] > plus_di[-1] and adx[-1] >= params.adx_minimum:
        return OrbVwapSetup(OptionSide.PE, opening_high, opening_low, current_vwap, adx[-1], bar.close_time)
    return None


def _within(moment: datetime, start: str, end: str) -> bool:
    start_hour, start_minute = (int(value) for value in start.split(":"))
    end_hour, end_minute = (int(value) for value in end.split(":"))
    value = moment.hour * 60 + moment.minute
    return start_hour * 60 + start_minute <= value <= end_hour * 60 + end_minute
