"""Research-only 9/20 EMA pullback scalp for the stored Indian index archive.

The source playbook describes a trend-aligned pullback: 9 EMA above/below the
20 EMA, a touch of either EMA, then a confirming reversal candle.  The spot
signal is calculated from resampled M1 index candles; it is not inferred from
the sparsely sampled option chain.  Option entries and exits remain
conservative: next available ask to enter, displayed bid to exit, and the full
configured NSE option cost schedule.

This module is intentionally not wired into OptionMaster's forward trader.
It exists to evaluate the idea before any paper-trial decision is made.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.backtest.scalper import SimulatedTrade, _parse_time
from optionmaster.backtest.spot import SpotCandle, snapshot_at_or_after
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class PullbackParams(BaseModel):
    strategy_id: str = Field(default="stored-pullback-v1", pattern=r"^[a-z0-9-]{3,64}$")
    bar_minutes: int = Field(default=5, ge=5, le=30)
    fast_ema: int = Field(default=9, ge=2, le=50)
    slow_ema: int = Field(default=20, ge=5, le=100)
    trend_slope_bars: int = Field(default=3, ge=1, le=10)
    structure_window: int = Field(default=10, ge=3, le=30)
    minimum_ema_spread_pct: float = Field(default=0.025, ge=0, le=1.0)
    stop_loss_pct: float = Field(default=10.0, gt=0, le=50.0)
    target_pct: float = Field(default=15.0, gt=0, le=100.0)
    max_hold_minutes: float = Field(default=12.0, ge=5.0, le=120.0)
    entry_start: str = Field(default="10:00", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field(default="13:00", pattern=r"^\d{2}:\d{2}$")
    squareoff_time: str = Field(default="15:12", pattern=r"^\d{2}:\d{2}$")
    max_spread_pct: float = Field(default=1.0, gt=0, le=5.0)
    min_premium: float = Field(default=20.0, ge=0)
    max_premium: float = Field(default=600.0, gt=0)
    cooldown_minutes: float = Field(default=5.0, ge=0, le=60.0)
    max_trades_per_day: int = Field(default=4, ge=1, le=20)
    fill_tolerance_seconds: float = Field(default=150.0, ge=60.0, le=600.0)
    use_index_option_context: bool = True
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)


class _Bar:
    __slots__ = ("timestamp", "open", "high", "low", "close", "minutes")

    def __init__(
        self, *, timestamp: datetime, open: float, high: float, low: float, close: float, minutes: int
    ) -> None:
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.minutes = minutes

    @property
    def close_time(self) -> datetime:
        return self.timestamp + timedelta(minutes=self.minutes)

    @property
    def range(self) -> float:
        return self.high - self.low


class _OpenPosition:
    __slots__ = ("side", "strike", "entry_price", "entry_snapshot", "stop_loss", "target")

    def __init__(
        self, *, side: str, strike: float, entry_price: float,
        entry_snapshot: ChainSnapshot, stop_loss: float, target: float,
    ) -> None:
        self.side = side
        self.strike = strike
        self.entry_price = entry_price
        self.entry_snapshot = entry_snapshot
        self.stop_loss = stop_loss
        self.target = target


def _resample(candles: list[SpotCandle], minutes: int) -> list[_Bar]:
    """Aggregate complete M1 bars, aligned to the 09:15 NSE session open."""
    groups: list[list[SpotCandle]] = []
    current_key: tuple[int, int] | None = None
    for candle in candles:
        elapsed = (candle.timestamp.hour * 60 + candle.timestamp.minute) - (9 * 60 + 15)
        if elapsed < 0:
            continue
        key = (candle.timestamp.hour, 15 + (elapsed // minutes) * minutes)
        # The session does not cross an hour for a 5/15-minute bucket, but
        # normalise the key so it also supports other allowed values.
        key = ((9 * 60 + 15 + (elapsed // minutes) * minutes) // 60,
               (9 * 60 + 15 + (elapsed // minutes) * minutes) % 60)
        if key != current_key:
            groups.append([])
            current_key = key
        groups[-1].append(candle)
    return [
        _Bar(
            timestamp=items[0].timestamp,
            open=items[0].open,
            high=max(item.high for item in items),
            low=min(item.low for item in items),
            close=items[-1].close,
            minutes=minutes,
        )
        for items in groups
        if len(items) == minutes
    ]


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result: list[float] = []
    previous: float | None = None
    for value in values:
        previous = value if previous is None else (alpha * value) + ((1 - alpha) * previous)
        result.append(previous)
    return result


def _signal(
    bars: list[_Bar], index: int, fast: list[float], slow: list[float], params: PullbackParams
) -> str | None:
    """Return CE/PE only after a complete trend, touch, and rejection sequence."""
    if index < max(params.slow_ema, params.structure_window * 2, params.trend_slope_bars):
        return None
    bar = bars[index]
    if bar.range <= 0:
        return None
    structure = params.structure_window
    recent = bars[index - structure + 1:index + 1]
    prior = bars[index - structure * 2 + 1:index - structure + 1]
    if len(recent) != structure or len(prior) != structure:
        return None
    spread_pct = abs(fast[index] - slow[index]) / slow[index] * 100 if slow[index] else 0
    structural_up = max(item.high for item in recent) > max(item.high for item in prior) and \
        min(item.low for item in recent) > min(item.low for item in prior)
    structural_down = max(item.high for item in recent) < max(item.high for item in prior) and \
        min(item.low for item in recent) < min(item.low for item in prior)
    previous_close = bars[index - 1].close
    slope_index = index - params.trend_slope_bars

    if (
        fast[index] > slow[index]
        and slow[index] > slow[slope_index]
        and spread_pct >= params.minimum_ema_spread_pct
        and structural_up
        and (bar.low <= fast[index] or bar.low <= slow[index])
        and bar.close > bar.open
        and bar.close > previous_close
    ):
        return "CE"
    near_low = (bar.close - bar.low) / bar.range <= 0.15
    if (
        fast[index] < slow[index]
        and slow[index] < slow[slope_index]
        and spread_pct >= params.minimum_ema_spread_pct
        and structural_down
        and (bar.high >= fast[index] or bar.high >= slow[index])
        and bar.close < bar.open
        and near_low
    ):
        return "PE"
    return None


def simulate_day(
    day: StoredDay,
    candles: list[SpotCandle],
    params: PullbackParams,
    *,
    lot_size: int,
    lots: int = 1,
    calculator: NseOptionCostCalculator,
) -> list[SimulatedTrade]:
    """Replay one stored day. Signals use only candles already closed at entry."""
    snapshots = day.snapshots
    bars = _resample(candles, params.bar_minutes)
    if len(snapshots) < 5 or len(bars) < params.slow_ema + params.structure_window * 2:
        return []
    fast = _ema([bar.close for bar in bars], params.fast_ema)
    slow = _ema([bar.close for bar in bars], params.slow_ema)
    quantity = lot_size * lots
    entry_start = _parse_time(params.entry_start)
    entry_end = _parse_time(params.entry_end)
    squareoff = _parse_time(params.squareoff_time)
    trades: list[SimulatedTrade] = []
    position: _OpenPosition | None = None
    last_exit_at: datetime | None = None

    def close(current: ChainSnapshot, price: float, reason: str) -> None:
        nonlocal position, last_exit_at
        assert position is not None
        result = calculator.net_result(
            entry_price=position.entry_price, exit_price=price, quantity=quantity, underlying=day.symbol
        )
        trades.append(SimulatedTrade(
            symbol=day.symbol, trade_date=day.day.isoformat(), expiry=current.expiry,
            side=position.side, strike=position.strike, quantity=quantity, lot_size=lot_size,
            entry_time=position.entry_snapshot.timestamp, exit_time=current.timestamp,
            entry_price=position.entry_price, exit_price=price, exit_reason=reason,
            hold_minutes=round((current.timestamp - position.entry_snapshot.timestamp).total_seconds() / 60, 2),
            spot_entry=position.entry_snapshot.spot, spot_exit=current.spot,
            gross_pnl=result.gross_pnl, charges=result.costs.total, net_pnl=result.net_pnl,
        ))
        last_exit_at = current.timestamp
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
        if not entry_start <= bar.close_time.time() <= entry_end:
            continue
        if last_exit_at is not None and bar.close_time - last_exit_at < timedelta(minutes=params.cooldown_minutes):
            continue
        side = _signal(bars, index, fast, slow, params)
        if side is None:
            continue
        snapshot = snapshot_at_or_after(
            snapshots, bar.close_time, tolerance_seconds=params.fill_tolerance_seconds
        )
        if snapshot is None:
            continue
        setup = select_and_confirm(
            snapshots=snapshots, snapshot=snapshot,
            candles=candles,
            side=side, min_premium=params.min_premium, max_premium=params.max_premium,
            max_spread_pct=params.max_spread_pct, min_delta=params.minimum_option_delta,
            max_delta=params.maximum_option_delta, target_r=1.5,
        ) if params.use_index_option_context else None
        strike = setup.strike if setup else snapshot.atm_strike(side)
        if strike is None:
            continue
        quote = snapshot.quote(strike, side)
        if quote is None or quote.ask <= 0 or quote.bid <= 0:
            continue
        if quote.ltp < params.min_premium or quote.ask > params.max_premium:
            continue
        if quote.spread_pct is None or quote.spread_pct > params.max_spread_pct:
            continue
        position = _OpenPosition(
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
