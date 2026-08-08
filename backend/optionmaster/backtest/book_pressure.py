"""Research replay for the Book Pressure option-buying scalp.

The source strategy uses an OHLCV pressure proxy, not true Level-2 data.  This
module keeps that limitation explicit and is for backtest comparison only.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.backtest.scalper import SimulatedTrade, _parse_time
from optionmaster.backtest.spot import SpotCandle, snapshot_at_or_after
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class BookPressureParams(BaseModel):
    strategy_id: str = Field(default="book-pressure-option-v1", pattern=r"^[a-z0-9-]{3,64}$")
    ema_period: int = Field(default=21, ge=5, le=50)
    rsi_period: int = Field(default=9, ge=2, le=30)
    atr_period: int = Field(default=14, ge=5, le=50)
    pressure_threshold: float = Field(default=0.65, gt=0.5, lt=1)
    volume_multiple: float = Field(default=0.8, gt=0, le=2)
    stop_atr_multiple: float = Field(default=1.0, gt=0, le=3)
    target_r: float = Field(default=1.5, gt=0, le=5)
    max_hold_minutes: float = Field(default=30.0, ge=5, le=120)
    entry_start: str = Field(default="09:30", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field(default="14:30", pattern=r"^\d{2}:\d{2}$")
    squareoff_time: str = Field(default="15:12", pattern=r"^\d{2}:\d{2}$")
    max_spread_pct: float = Field(default=1.0, gt=0, le=5)
    min_premium: float = Field(default=20.0, ge=0)
    max_premium: float = Field(default=600.0, gt=0)
    cooldown_minutes: float = Field(default=5.0, ge=0, le=60.0)
    max_trades_per_day: int = Field(default=3, ge=1, le=10)
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
    spot_stop: float
    spot_target: float


def simulate_day(day: StoredDay, candles: list[SpotCandle], params: BookPressureParams, *, lot_size: int, lots: int = 1, calculator: NseOptionCostCalculator) -> list[SimulatedTrade]:
    if len(day.snapshots) < 5 or len(candles) <= params.ema_period:
        return []
    closes = [item.close for item in candles]
    ema = _ema(closes, params.ema_period)
    rsi = _rsi(closes, params.rsi_period)
    atr = _atr(candles, params.atr_period)
    snapshots, quantity = day.snapshots, lot_size * lots
    entry_start, entry_end, squareoff = _parse_time(params.entry_start), _parse_time(params.entry_end), _parse_time(params.squareoff_time)
    trades: list[SimulatedTrade] = []
    position: _Position | None = None
    last_exit: datetime | None = None

    def close(snapshot: ChainSnapshot, price: float, reason: str) -> None:
        nonlocal position, last_exit
        assert position is not None
        result = calculator.net_result(entry_price=position.entry_price, exit_price=price, quantity=quantity, underlying=day.symbol)
        trades.append(SimulatedTrade(symbol=day.symbol, trade_date=day.day.isoformat(), expiry=snapshot.expiry, side=position.side, strike=position.strike, quantity=quantity, lot_size=lot_size, entry_time=position.entry_snapshot.timestamp, exit_time=snapshot.timestamp, entry_price=position.entry_price, exit_price=price, exit_reason=reason, hold_minutes=round((snapshot.timestamp-position.entry_snapshot.timestamp).total_seconds()/60, 2), spot_entry=position.entry_snapshot.spot, spot_exit=snapshot.spot, gross_pnl=result.gross_pnl, charges=result.costs.total, net_pnl=result.net_pnl))
        last_exit, position = snapshot.timestamp, None

    for index, candle in enumerate(candles):
        if position is not None:
            snapshot = snapshot_at_or_after(snapshots, candle.close_time, tolerance_seconds=params.fill_tolerance_seconds)
            if snapshot is not None:
                quote = snapshot.quote(position.strike, position.side)
                bid = quote.bid if quote is not None and quote.bid > 0 else None
                if bid is not None:
                    stopped = bid <= position.spot_stop if params.use_index_option_context else candle.low <= position.spot_stop if position.side == "CE" else candle.high >= position.spot_stop
                    targeted = bid >= position.spot_target if params.use_index_option_context else candle.high >= position.spot_target if position.side == "CE" else candle.low <= position.spot_target
                    if stopped:
                        close(snapshot, bid, "SPOT_STOP")
                    elif targeted:
                        close(snapshot, bid, "SPOT_TARGET")
                    elif snapshot.timestamp - position.entry_snapshot.timestamp >= timedelta(minutes=params.max_hold_minutes):
                        close(snapshot, bid, "TIME_STOP")
                    elif snapshot.timestamp.time() >= squareoff:
                        close(snapshot, bid, "SQUARE_OFF")
            continue
        if index < max(params.ema_period, params.atr_period, 20) or len(trades) >= params.max_trades_per_day:
            continue
        if not entry_start <= candle.close_time.time() <= entry_end or (last_exit and candle.close_time-last_exit < timedelta(minutes=params.cooldown_minutes)):
            continue
        side = _signal(candles, index, ema, rsi, atr, params)
        if side is None:
            continue
        snapshot = snapshot_at_or_after(snapshots, candle.close_time, tolerance_seconds=params.fill_tolerance_seconds)
        if snapshot is None:
            continue
        setup = select_and_confirm(
            snapshots=snapshots, snapshot=snapshot, candles=candles, side=side,
            min_premium=params.min_premium, max_premium=params.max_premium,
            max_spread_pct=params.max_spread_pct, min_delta=params.minimum_option_delta,
            max_delta=params.maximum_option_delta, target_r=params.target_r,
        ) if params.use_index_option_context else None
        strike = setup.strike if setup else snapshot.atm_strike(side)
        quote = snapshot.quote(strike, side) if strike is not None else None
        if quote is None or quote.ask <= 0 or quote.bid <= 0 or quote.ltp < params.min_premium or quote.ask > params.max_premium or quote.spread_pct is None or quote.spread_pct > params.max_spread_pct:
            continue
        risk = atr[index] * params.stop_atr_multiple
        position = _Position(side=side, strike=strike, entry_price=setup.entry_price if setup else quote.ask, entry_snapshot=snapshot, spot_stop=setup.stop_loss if setup else candle.close-risk if side == "CE" else candle.close+risk, spot_target=setup.target if setup else candle.close+risk*params.target_r if side == "CE" else candle.close-risk*params.target_r)

    if position is not None:
        for snapshot in reversed(snapshots):
            quote = snapshot.quote(position.strike, position.side)
            if quote is not None and quote.bid > 0:
                close(snapshot, quote.bid, "DATA_END")
                break
    return trades


def _signal(candles, index, ema, rsi, atr, params) -> str | None:
    current, prior = candles[index], candles[index-1]
    if None in (ema[index], rsi[index], atr[index]) or atr[index] <= 0 or current.range < 0.5 * atr[index]:
        return None
    volumes = [item.volume for item in candles[index-20:index]]
    if current.volume < sum(volumes) / len(volumes) * params.volume_multiple:
        return None
    pressure = lambda item: (item.close-item.low)/item.range if item.range > 0 else 0.5
    if pressure(current) > params.pressure_threshold and pressure(prior) > params.pressure_threshold and current.close > ema[index] and rsi[index] < 70:
        return "CE"
    if pressure(current) < 1-params.pressure_threshold and pressure(prior) < 1-params.pressure_threshold and current.close < ema[index] and rsi[index] > 30:
        return "PE"
    return None


def _ema(values, period):
    result = [None] * len(values)
    factor = 2 / (period + 1)
    for index in range(period-1, len(values)):
        result[index] = sum(values[:period]) / period if index == period-1 else values[index]*factor + result[index-1]*(1-factor)
    return result


def _rsi(values, period):
    result = [None] * len(values)
    for index in range(period, len(values)):
        changes = [values[item]-values[item-1] for item in range(index-period+1, index+1)]
        gain, loss = sum(max(value, 0) for value in changes)/period, sum(max(-value, 0) for value in changes)/period
        result[index] = 100 if loss == 0 else 100 - 100/(1+gain/loss)
    return result


def _atr(candles, period):
    result = [None] * len(candles)
    for index in range(period, len(candles)):
        values = [max(candles[item].high-candles[item].low, abs(candles[item].high-candles[item-1].close), abs(candles[item].low-candles[item-1].close)) for item in range(index-period+1, index+1)]
        result[index] = sum(values)/period
    return result
