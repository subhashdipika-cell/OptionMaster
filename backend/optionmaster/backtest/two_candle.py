"""Cost-aware, option-premium replay of the Two Candle Theory.

This is a paper-research implementation of the Indian index option-buying
method documented in the local strategy vault.  It reads M1 spot OHLCV for the
signal and Dhan option-chain snapshots for conservative option fills: enter at
the next displayed ask and exit at the next displayed bid.  It is intentionally
not connected to the autonomous live order path.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.backtest.scalper import SimulatedTrade, _parse_time
from optionmaster.backtest.spot import SpotCandle, snapshot_at_or_after
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class TwoCandleParams(BaseModel):
    strategy_id: str = Field(default="two-candle-option-v1", pattern=r"^[a-z0-9-]{3,64}$")
    rsi_period: int = Field(default=14, ge=2, le=50)
    volume_median_bars: int = Field(default=40, ge=20, le=120)
    volume_surge_multiple: float = Field(default=2.0, ge=1.0, le=5.0)
    oi_lookback_minutes: float = Field(default=6.0, ge=2.0, le=30.0)
    stop_target_r: float = Field(default=2.0, ge=1.0, le=5.0)
    max_hold_minutes: float = Field(default=45.0, ge=5.0, le=180.0)
    entry_start: str = Field(default="09:45", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field(default="14:30", pattern=r"^\d{2}:\d{2}$")
    squareoff_time: str = Field(default="15:12", pattern=r"^\d{2}:\d{2}$")
    max_spread_pct: float = Field(default=1.0, gt=0, le=5.0)
    min_premium: float = Field(default=20.0, ge=0)
    max_premium: float = Field(default=600.0, gt=0)
    cooldown_minutes: float = Field(default=12.0, ge=0, le=60.0)
    max_trades_per_day: int = Field(default=2, ge=1, le=5)
    fill_tolerance_seconds: float = Field(default=150.0, ge=60.0, le=600.0)
    use_index_option_context: bool = True
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)


@dataclass(slots=True)
class _Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class _Position:
    side: str
    strike: float
    entry_price: float
    entry_snapshot: ChainSnapshot
    spot_stop: float
    spot_target: float


def simulate_day(
    day: StoredDay,
    candles: list[SpotCandle],
    params: TwoCandleParams,
    *,
    lot_size: int,
    lots: int = 1,
    calculator: NseOptionCostCalculator,
) -> list[SimulatedTrade]:
    """Replay a closed-bar Two Candle signal without lookahead."""
    if len(day.snapshots) < 5 or len(candles) < 75:
        return []
    bars = _resample_three_minutes(candles)
    if len(bars) < 25:
        return []
    rsi = _rsi([bar.close for bar in bars], params.rsi_period)
    atr = _atr(bars, 10)
    supertrend = _supertrend(bars, atr, 2.0)
    sar = _parabolic_sar(bars)
    vwma = _vwma(bars, 20)
    vwap = _session_vwap(bars)
    snapshots = day.snapshots
    quantity = lot_size * lots
    entry_start = _parse_time(params.entry_start)
    entry_end = _parse_time(params.entry_end)
    squareoff = _parse_time(params.squareoff_time)
    trades: list[SimulatedTrade] = []
    position: _Position | None = None
    last_exit: datetime | None = None

    def close(snapshot: ChainSnapshot, price: float, reason: str) -> None:
        nonlocal position, last_exit
        assert position is not None
        result = calculator.net_result(
            entry_price=position.entry_price,
            exit_price=price,
            quantity=quantity,
            underlying=day.symbol,
        )
        trades.append(
            SimulatedTrade(
                symbol=day.symbol,
                trade_date=day.day.isoformat(),
                expiry=snapshot.expiry,
                side=position.side,
                strike=position.strike,
                quantity=quantity,
                lot_size=lot_size,
                entry_time=position.entry_snapshot.timestamp,
                exit_time=snapshot.timestamp,
                entry_price=position.entry_price,
                exit_price=price,
                exit_reason=reason,
                hold_minutes=round((snapshot.timestamp - position.entry_snapshot.timestamp).total_seconds() / 60, 2),
                spot_entry=position.entry_snapshot.spot,
                spot_exit=snapshot.spot,
                gross_pnl=result.gross_pnl,
                charges=result.costs.total,
                net_pnl=result.net_pnl,
            )
        )
        last_exit = snapshot.timestamp
        position = None

    for index, bar in enumerate(bars):
        if position is not None:
            snapshot = snapshot_at_or_after(
                snapshots, bar.end, tolerance_seconds=params.fill_tolerance_seconds
            )
            if snapshot is not None:
                quote = snapshot.quote(position.strike, position.side)
                bid = quote.bid if quote is not None and quote.bid > 0 else None
                if bid is not None:
                    # If a compressed three-minute candle touches both levels,
                    # stop first: no intrabar sequence is available to claim a target.
                    stopped = (
                        bid <= position.spot_stop
                        if params.use_index_option_context
                        else bar.low <= position.spot_stop if position.side == "CE" else bar.high >= position.spot_stop
                    )
                    targeted = (
                        bid >= position.spot_target
                        if params.use_index_option_context
                        else bar.high >= position.spot_target if position.side == "CE" else bar.low <= position.spot_target
                    )
                    held = snapshot.timestamp - position.entry_snapshot.timestamp
                    if stopped:
                        close(snapshot, bid, "SPOT_STOP")
                    elif targeted:
                        close(snapshot, bid, "SPOT_TARGET")
                    elif held >= timedelta(minutes=params.max_hold_minutes):
                        close(snapshot, bid, "TIME_STOP")
                    elif snapshot.timestamp.time() >= squareoff:
                        close(snapshot, bid, "SQUARE_OFF")
            continue

        if len(trades) >= params.max_trades_per_day or index < max(21, params.volume_median_bars):
            continue
        if not entry_start <= bar.end.time() <= entry_end:
            continue
        if last_exit is not None and bar.end - last_exit < timedelta(minutes=params.cooldown_minutes):
            continue
        signal = _signal(
            bars=bars,
            index=index,
            rsi=rsi,
            supertrend=supertrend,
            sar=sar,
            vwma=vwma,
            vwap=vwap,
            snapshots=snapshots,
            params=params,
        )
        if signal is None:
            continue
        side, spot_stop, spot_target = signal
        snapshot = snapshot_at_or_after(
            snapshots, bar.end, tolerance_seconds=params.fill_tolerance_seconds
        )
        if snapshot is None:
            continue
        setup = select_and_confirm(
            snapshots=snapshots, snapshot=snapshot,
            candles=candles,
            side=side, min_premium=params.min_premium, max_premium=params.max_premium,
            max_spread_pct=params.max_spread_pct, min_delta=params.minimum_option_delta,
            max_delta=params.maximum_option_delta, target_r=params.stop_target_r,
        ) if params.use_index_option_context else None
        strike = setup.strike if setup else snapshot.atm_strike(side)
        quote = snapshot.quote(strike, side) if strike is not None else None
        if quote is None or quote.ask <= 0 or quote.bid <= 0:
            continue
        if quote.ltp < params.min_premium or quote.ask > params.max_premium:
            continue
        if quote.spread_pct is None or quote.spread_pct > params.max_spread_pct:
            continue
        position = _Position(
            side=side,
            strike=strike,
            entry_price=setup.entry_price if setup else quote.ask,
            entry_snapshot=snapshot,
            spot_stop=setup.stop_loss if setup else spot_stop,
            spot_target=setup.target if setup else spot_target,
        )

    if position is not None:
        for snapshot in reversed(snapshots):
            quote = snapshot.quote(position.strike, position.side)
            if quote is not None and quote.bid > 0:
                close(snapshot, quote.bid, "DATA_END")
                break
    return trades


def _signal(*, bars, index, rsi, supertrend, sar, vwma, vwap, snapshots, params) -> tuple[str, float, float] | None:
    previous, current = bars[index - 1], bars[index]
    if not _volume_surge(bars, index, params):
        return None
    if rsi[index] is None or not _oi_bias(snapshots, current.end, params.oi_lookback_minutes):
        return None
    bullish = _above_all(previous, index - 1, supertrend, sar, vwma, vwap) and _above_all(current, index, supertrend, sar, vwma, vwap)
    bearish = _below_all(previous, index - 1, supertrend, sar, vwma, vwap) and _below_all(current, index, supertrend, sar, vwma, vwap)
    bias = _oi_bias(snapshots, current.end, params.oi_lookback_minutes)
    if bullish and bias == "bull" and 50 <= rsi[index] <= 80:
        risk = current.close - previous.low
        if risk > 0:
            return "CE", previous.low, current.close + risk * params.stop_target_r
    if bearish and bias == "bear" and 20 <= rsi[index] <= 40:
        risk = previous.high - current.close
        if risk > 0:
            return "PE", previous.high, current.close - risk * params.stop_target_r
    return None


def _above_all(bar, index, supertrend, sar, vwma, vwap) -> bool:
    values = (supertrend[index], sar[index], vwma[index], vwap[index])
    return all(value is not None and bar.close > value for value in values)


def _below_all(bar, index, supertrend, sar, vwma, vwap) -> bool:
    values = (supertrend[index], sar[index], vwma[index], vwap[index])
    return all(value is not None and bar.close < value for value in values)


def _volume_surge(bars: list[_Bar], index: int, params: TwoCandleParams) -> bool:
    history = [bar.volume for bar in bars[index - params.volume_median_bars:index]]
    if len(history) < params.volume_median_bars:
        return False
    floor = median(history) * params.volume_surge_multiple
    return bars[index - 1].volume >= floor and bars[index].volume >= floor


def _oi_bias(snapshots: list[ChainSnapshot], moment: datetime, lookback_minutes: float) -> str | None:
    current = next((item for item in reversed(snapshots) if item.timestamp <= moment), None)
    if current is None or (moment - current.timestamp).total_seconds() > 240:
        return None
    target = moment - timedelta(minutes=lookback_minutes)
    prior = next((item for item in reversed(snapshots) if item.timestamp <= target), None)
    if prior is None:
        return None
    def totals(snapshot: ChainSnapshot) -> tuple[float, float]:
        return (
            sum(item.oi for item in snapshot.quotes.values() if item.side == "CE"),
            sum(item.oi for item in snapshot.quotes.values() if item.side == "PE"),
        )
    current_ce, current_pe = totals(current)
    prior_ce, prior_pe = totals(prior)
    call_change, put_change = current_ce - prior_ce, current_pe - prior_pe
    if put_change > 0 and put_change > call_change:
        return "bull"
    if call_change > 0 and call_change > put_change:
        return "bear"
    return None


def _resample_three_minutes(candles: list[SpotCandle]) -> list[_Bar]:
    buckets: list[_Bar] = []
    current: _Bar | None = None
    for candle in candles:
        offset = (candle.timestamp.hour * 60 + candle.timestamp.minute) // 3
        start = candle.timestamp.replace(minute=(offset * 3) % 60, second=0, microsecond=0)
        if current is None or current.start != start:
            if current is not None:
                buckets.append(current)
            current = _Bar(start, start + timedelta(minutes=3), candle.open, candle.high, candle.low, candle.close, candle.volume)
        else:
            current.high = max(current.high, candle.high)
            current.low = min(current.low, candle.low)
            current.close = candle.close
            current.volume += candle.volume
    if current is not None:
        buckets.append(current)
    return buckets


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(period, len(values)):
        changes = [values[item] - values[item - 1] for item in range(index - period + 1, index + 1)]
        gains = sum(max(change, 0) for change in changes) / period
        losses = sum(max(-change, 0) for change in changes) / period
        result[index] = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    return result


def _atr(bars: list[_Bar], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    for index in range(period, len(bars)):
        ranges = [max(bars[item].high - bars[item].low, abs(bars[item].high - bars[item - 1].close), abs(bars[item].low - bars[item - 1].close)) for item in range(index - period + 1, index + 1)]
        result[index] = sum(ranges) / period
    return result


def _supertrend(bars: list[_Bar], atr: list[float | None], multiplier: float) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    upper_final = lower_final = None
    rising = True
    for index, bar in enumerate(bars):
        if atr[index] is None:
            continue
        midpoint = (bar.high + bar.low) / 2
        upper = midpoint + multiplier * atr[index]
        lower = midpoint - multiplier * atr[index]
        prior_close = bars[index - 1].close if index else bar.close
        upper_final = upper if upper_final is None or upper < upper_final or prior_close > upper_final else upper_final
        lower_final = lower if lower_final is None or lower > lower_final or prior_close < lower_final else lower_final
        if result[index - 1] is None if index else True:
            rising = bar.close >= midpoint
        elif rising and bar.close < lower_final:
            rising, upper_final = False, upper
        elif not rising and bar.close > upper_final:
            rising, lower_final = True, lower
        result[index] = lower_final if rising else upper_final
    return result


def _parabolic_sar(bars: list[_Bar], step: float = 0.02, maximum: float = 0.2) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) < 2:
        return result
    rising = bars[1].close >= bars[0].close
    result[1] = bars[0].low if rising else bars[0].high
    extreme = bars[1].high if rising else bars[1].low
    factor = step
    for index in range(2, len(bars)):
        value = result[index - 1] + factor * (extreme - result[index - 1])
        if rising:
            value = min(value, bars[index - 1].low, bars[index - 2].low)
            if bars[index].low < value:
                rising, value, extreme, factor = False, extreme, bars[index].low, step
            elif bars[index].high > extreme:
                extreme, factor = bars[index].high, min(factor + step, maximum)
        else:
            value = max(value, bars[index - 1].high, bars[index - 2].high)
            if bars[index].high > value:
                rising, value, extreme, factor = True, extreme, bars[index].high, step
            elif bars[index].low < extreme:
                extreme, factor = bars[index].low, min(factor + step, maximum)
        result[index] = value
    return result


def _vwma(bars: list[_Bar], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    for index in range(period - 1, len(bars)):
        window = bars[index - period + 1:index + 1]
        volume = sum(bar.volume for bar in window)
        if volume > 0:
            result[index] = sum(bar.close * bar.volume for bar in window) / volume
    return result


def _session_vwap(bars: list[_Bar]) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    day = None
    price_volume = volume = 0.0
    for index, bar in enumerate(bars):
        if bar.start.date() != day:
            day, price_volume, volume = bar.start.date(), 0.0, 0.0
        price = (bar.high + bar.low + bar.close) / 3
        price_volume += price * bar.volume
        volume += bar.volume
        result[index] = price_volume / volume if volume > 0 else None
    return result
