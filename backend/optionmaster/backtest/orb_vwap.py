"""Research-only opening-range breakout (ORB) option-buying replay.

This is a deliberately fixed first candidate distilled from the local trading
library, not an optimiser.  It buys an ATM index option only when a completed
five-minute bar breaks the 09:15--09:30 opening range, agrees with session
VWAP and an ADX/+DI/-DI trend reading, and has above-opening-range volume.
The option must also be liquid and moving in the same direction.  Entries use
the next archived ask, exits use the displayed bid, and full NSE costs apply.

The archived chain is sampled about once a minute.  Therefore this evaluates
the idea conservatively at archive resolution; it is not connected to a
forward monitor, paper trader, or live order route.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.backtest.pullback import _resample
from optionmaster.backtest.scalper import SimulatedTrade, _parse_time
from optionmaster.backtest.spot import SpotCandle, snapshot_at_or_after
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class OrbVwapParams(BaseModel):
    """Fixed, pre-declared ORB/VWAP candidate settings for research replay."""

    strategy_id: str = Field(default="stored-orb-vwap-v1", pattern=r"^[a-z0-9-]{3,64}$")
    opening_range_minutes: int = Field(default=15, ge=15, le=30)
    bar_minutes: int = Field(default=5, ge=5, le=15)
    adx_period: int = Field(default=14, ge=5, le=30)
    adx_minimum: float = Field(default=20.0, ge=5, le=60)
    volume_multiple: float = Field(default=1.25, ge=1.0, le=5.0)
    option_momentum_minimum_pct: float = Field(default=0.15, ge=0, le=5.0)
    stop_loss_pct: float = Field(default=5.0, gt=0, le=50.0)
    target_pct: float = Field(default=10.0, gt=0, le=100.0)
    max_hold_minutes: float = Field(default=20.0, ge=5.0, le=120.0)
    entry_start: str = Field(default="09:30", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field(default="10:45", pattern=r"^\d{2}:\d{2}$")
    squareoff_time: str = Field(default="15:12", pattern=r"^\d{2}:\d{2}$")
    max_spread_pct: float = Field(default=1.0, gt=0, le=5.0)
    min_premium: float = Field(default=20.0, ge=0)
    max_premium: float = Field(default=600.0, gt=0)
    max_trades_per_day: int = Field(default=1, ge=1, le=5)
    fill_tolerance_seconds: float = Field(default=150.0, ge=60.0, le=600.0)
    use_index_option_context: bool = True
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)


@dataclass(slots=True)
class _Position:
    side: str
    strike: float
    entry_price: float
    entry_snapshot: ChainSnapshot
    stop_loss: float
    target: float


def _session_vwap(candles: list[SpotCandle]) -> list[float | None]:
    result: list[float | None] = []
    price_volume = volume = 0.0
    for candle in candles:
        typical = (candle.high + candle.low + candle.close) / 3
        price_volume += typical * candle.volume
        volume += candle.volume
        result.append(price_volume / volume if volume > 0 else None)
    return result


def _directional_movement(candles: list[SpotCandle], period: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Wilder-style +DI, -DI and ADX from closed M1 candles."""
    length = len(candles)
    plus_di: list[float | None] = [None] * length
    minus_di: list[float | None] = [None] * length
    adx: list[float | None] = [None] * length
    if length <= period * 2:
        return plus_di, minus_di, adx

    true_ranges: list[float] = [0.0]
    plus_moves: list[float] = [0.0]
    minus_moves: list[float] = [0.0]
    for index in range(1, length):
        current, prior = candles[index], candles[index - 1]
        upward = current.high - prior.high
        downward = prior.low - current.low
        plus_moves.append(upward if upward > downward and upward > 0 else 0.0)
        minus_moves.append(downward if downward > upward and downward > 0 else 0.0)
        true_ranges.append(max(current.high - current.low, abs(current.high - prior.close), abs(current.low - prior.close)))

    smooth_tr = sum(true_ranges[1:period + 1])
    smooth_plus = sum(plus_moves[1:period + 1])
    smooth_minus = sum(minus_moves[1:period + 1])
    dx: list[float | None] = [None] * length
    for index in range(period, length):
        if index > period:
            smooth_tr = smooth_tr - (smooth_tr / period) + true_ranges[index]
            smooth_plus = smooth_plus - (smooth_plus / period) + plus_moves[index]
            smooth_minus = smooth_minus - (smooth_minus / period) + minus_moves[index]
        if smooth_tr <= 0:
            continue
        plus = 100 * smooth_plus / smooth_tr
        minus = 100 * smooth_minus / smooth_tr
        plus_di[index], minus_di[index] = plus, minus
        total = plus + minus
        dx[index] = 100 * abs(plus - minus) / total if total else 0.0

    first_adx_index = period * 2 - 1
    seed = [value for value in dx[period:first_adx_index + 1] if value is not None]
    if len(seed) < period:
        return plus_di, minus_di, adx
    adx[first_adx_index] = sum(seed) / period
    for index in range(first_adx_index + 1, length):
        if dx[index] is not None and adx[index - 1] is not None:
            adx[index] = ((adx[index - 1] * (period - 1)) + dx[index]) / period
    return plus_di, minus_di, adx


def _minute_index_at_or_before(candles: list[SpotCandle], moment: datetime) -> int | None:
    result = None
    for index, candle in enumerate(candles):
        if candle.close_time <= moment:
            result = index
        else:
            break
    return result


def _option_has_momentum(snapshots: list[ChainSnapshot], snapshot: ChainSnapshot, strike: float, side: str, minimum: float) -> bool:
    prior = next((item for item in reversed(snapshots) if item.timestamp < snapshot.timestamp), None)
    if prior is None:
        return False
    current_quote, prior_quote = snapshot.quote(strike, side), prior.quote(strike, side)
    if current_quote is None or prior_quote is None or prior_quote.ltp <= 0:
        return False
    return ((current_quote.ltp / prior_quote.ltp) - 1) * 100 >= minimum


def simulate_day(
    day: StoredDay,
    candles: list[SpotCandle,
    ],
    params: OrbVwapParams,
    *,
    lot_size: int,
    lots: int = 1,
    calculator: NseOptionCostCalculator,
) -> list[SimulatedTrade]:
    """Replay one day using information available only at each completed bar."""
    snapshots = day.snapshots
    if len(snapshots) < 5 or len(candles) < (params.adx_period * 2):
        return []
    bars = _resample(candles, params.bar_minutes)
    opening_bars = params.opening_range_minutes // params.bar_minutes
    if len(bars) <= opening_bars:
        return []
    opening_high = max(bar.high for bar in bars[:opening_bars])
    opening_low = min(bar.low for bar in bars[:opening_bars])
    opening_volume = sum(candle.volume for candle in candles[:params.opening_range_minutes])
    vwap = _session_vwap(candles)
    plus_di, minus_di, adx = _directional_movement(candles, params.adx_period)
    quantity = lot_size * lots
    entry_start, entry_end, squareoff = (_parse_time(params.entry_start), _parse_time(params.entry_end), _parse_time(params.squareoff_time))
    trades: list[SimulatedTrade] = []
    position: _Position | None = None

    def close(snapshot: ChainSnapshot, price: float, reason: str) -> None:
        nonlocal position
        assert position is not None
        result = calculator.net_result(entry_price=position.entry_price, exit_price=price, quantity=quantity, underlying=day.symbol)
        trades.append(SimulatedTrade(
            symbol=day.symbol, trade_date=day.day.isoformat(), expiry=snapshot.expiry,
            side=position.side, strike=position.strike, quantity=quantity, lot_size=lot_size,
            entry_time=position.entry_snapshot.timestamp, exit_time=snapshot.timestamp,
            entry_price=position.entry_price, exit_price=price, exit_reason=reason,
            hold_minutes=round((snapshot.timestamp - position.entry_snapshot.timestamp).total_seconds() / 60, 2),
            spot_entry=position.entry_snapshot.spot, spot_exit=snapshot.spot,
            gross_pnl=result.gross_pnl, charges=result.costs.total, net_pnl=result.net_pnl,
        ))
        position = None

    def manage(until: datetime) -> None:
        nonlocal position
        if position is None:
            return
        for snapshot in snapshots:
            if position is None or snapshot.timestamp > until:
                break
            if snapshot.timestamp <= position.entry_snapshot.timestamp:
                continue
            quote = snapshot.quote(position.strike, position.side)
            bid = quote.bid if quote is not None and quote.bid > 0 else None
            if bid is None:
                continue
            if bid <= position.stop_loss:
                close(snapshot, bid, "STOP_LOSS")
            elif bid >= position.target:
                close(snapshot, bid, "TARGET")
            elif snapshot.timestamp - position.entry_snapshot.timestamp >= timedelta(minutes=params.max_hold_minutes):
                close(snapshot, bid, "TIME_STOP")
            elif snapshot.timestamp.time() >= squareoff:
                close(snapshot, bid, "SQUARE_OFF")

    for index, bar in enumerate(bars):
        manage(bar.close_time)
        if position is not None or len(trades) >= params.max_trades_per_day:
            continue
        if index < opening_bars or not entry_start <= bar.close_time.time() <= entry_end:
            continue
        minute_index = _minute_index_at_or_before(candles, bar.close_time)
        if minute_index is None:
            continue
        current_vwap, current_plus, current_minus, current_adx = vwap[minute_index], plus_di[minute_index], minus_di[minute_index], adx[minute_index]
        if current_vwap is None or current_plus is None or current_minus is None or current_adx is None:
            continue
        bar_volume = sum(item.volume for item in candles if bar.timestamp <= item.timestamp < bar.close_time)
        if opening_volume <= 0 or bar_volume < (opening_volume / opening_bars) * params.volume_multiple:
            continue
        side = None
        if bar.close > opening_high and bar.close > current_vwap and current_plus > current_minus and current_adx >= params.adx_minimum:
            side = "CE"
        elif bar.close < opening_low and bar.close < current_vwap and current_minus > current_plus and current_adx >= params.adx_minimum:
            side = "PE"
        if side is None:
            continue
        snapshot = snapshot_at_or_after(snapshots, bar.close_time, tolerance_seconds=params.fill_tolerance_seconds)
        if snapshot is None:
            continue
        setup = select_and_confirm(
            snapshots=snapshots, snapshot=snapshot,
            candles=candles,
            side=side, min_premium=params.min_premium, max_premium=params.max_premium,
            max_spread_pct=params.max_spread_pct, min_delta=params.minimum_option_delta,
            max_delta=params.maximum_option_delta, target_r=1.8,
            require_confirmation=True,
        ) if params.use_index_option_context else None
        strike = setup.strike if setup else snapshot.atm_strike(side)
        quote = snapshot.quote(strike, side) if strike is not None else None
        if quote is None or quote.ask <= 0 or quote.bid <= 0 or quote.ltp < params.min_premium or quote.ask > params.max_premium:
            continue
        if quote.spread_pct is None or quote.spread_pct > params.max_spread_pct:
            continue
        if not _option_has_momentum(snapshots, snapshot, strike, side, params.option_momentum_minimum_pct):
            continue
        position = _Position(
            side=side, strike=strike, entry_price=setup.entry_price if setup else quote.ask, entry_snapshot=snapshot,
            stop_loss=setup.stop_loss if setup else round(quote.ask * (1 - params.stop_loss_pct / 100), 2),
            target=setup.target if setup else round(quote.ask * (1 + params.target_pct / 100), 2),
        )

    if position is not None:
        for snapshot in reversed(snapshots):
            quote = snapshot.quote(position.strike, position.side)
            if quote is not None and quote.bid > 0:
                close(snapshot, quote.bid, "DATA_END")
                break
    return trades
