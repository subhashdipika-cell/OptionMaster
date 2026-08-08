"""Research-only 5-minute breakout-retest option-buying scalp.

Definition fixed before replay:
1. A completed 5-minute bar closes beyond the high/low of the prior 12 bars
   (one hour) by at least ``minimum_breakout_pct``.
2. Within the next three completed bars, price revisits that broken level
   without invalidating it and closes back through it with a directional body.
3. A held upside level buys a CE; a held downside level buys a PE.

The M1 index candles create the signal.  The next Dhan chain snapshot provides
the buy ask, later snapshots provide the sell bid, and the full NSE cost model
is applied.  This module is research-only and has no forward-execution route.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.backtest.pullback import _resample
from optionmaster.backtest.scalper import SimulatedTrade, _parse_time
from optionmaster.backtest.spot import SpotCandle, snapshot_at_or_after
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class BreakoutRetestParams(BaseModel):
    strategy_id: str = Field(default="stored-breakout-retest-v1", pattern=r"^[a-z0-9-]{3,64}$")
    bar_minutes: int = Field(default=5, ge=5, le=15)
    range_lookback_bars: int = Field(default=12, ge=4, le=30)
    pattern: Literal["range", "three-bar"] = "range"
    minimum_breakout_pct: float = Field(default=0.05, gt=0, le=1.0)
    retest_bars: int = Field(default=3, ge=1, le=8)
    retest_tolerance_pct: float = Field(default=0.04, gt=0, le=0.5)
    invalidation_pct: float = Field(default=0.08, gt=0, le=0.5)
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
    require_option_momentum_confirmation: bool = True
    option_momentum_minimum_pct: float = Field(default=0.15, ge=0, le=5.0)
    use_index_option_context: bool = True
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)


@dataclass(slots=True)
class _PendingBreakout:
    side: str
    level: float
    detected_at: int


@dataclass(slots=True)
class _OpenPosition:
    side: str
    strike: float
    entry_price: float
    entry_snapshot: ChainSnapshot
    stop_loss: float
    target: float


def _confirmation(bar, pending: _PendingBreakout, params: BreakoutRetestParams) -> bool:
    """The retest must hold the reclaimed level and finish directionally."""
    if pending.side == "CE":
        return (
            bar.low <= pending.level * (1 + params.retest_tolerance_pct / 100)
            and bar.low >= pending.level * (1 - params.invalidation_pct / 100)
            and bar.close >= pending.level
            and bar.close > bar.open
        )
    return (
        bar.high >= pending.level * (1 - params.retest_tolerance_pct / 100)
        and bar.high <= pending.level * (1 + params.invalidation_pct / 100)
        and bar.close <= pending.level
        and bar.close < bar.open
    )


def _new_breakout(bars, index: int, params: BreakoutRetestParams) -> _PendingBreakout | None:
    if index < params.range_lookback_bars:
        return None
    bar = bars[index]
    prior = bars[index - params.range_lookback_bars:index]
    prior_high = max(item.high for item in prior)
    prior_low = min(item.low for item in prior)
    if bar.close >= prior_high * (1 + params.minimum_breakout_pct / 100):
        return _PendingBreakout(side="CE", level=prior_high, detected_at=index)
    if bar.close <= prior_low * (1 - params.minimum_breakout_pct / 100):
        return _PendingBreakout(side="PE", level=prior_low, detected_at=index)
    return None


def _three_bar_signal(bars, index: int, params: BreakoutRetestParams) -> str | None:
    """Reference bar -> breakout close -> retest and turn, with no lookahead."""
    if index < 2:
        return None
    reference, breakout, retest = bars[index - 2:index + 1]
    threshold = params.minimum_breakout_pct / 100
    if (
        breakout.close > reference.high * (1 + threshold)
        and retest.low <= reference.high * (1 + params.retest_tolerance_pct / 100)
        and retest.low >= reference.high * (1 - params.invalidation_pct / 100)
        and retest.close > reference.high and retest.close > retest.open
    ):
        return "CE"
    if (
        breakout.close < reference.low * (1 - threshold)
        and retest.high >= reference.low * (1 - params.retest_tolerance_pct / 100)
        and retest.high <= reference.low * (1 + params.invalidation_pct / 100)
        and retest.close < reference.low and retest.close < retest.open
    ):
        return "PE"
    return None


def simulate_day(
    day: StoredDay,
    candles: list[SpotCandle],
    params: BreakoutRetestParams,
    *,
    lot_size: int,
    lots: int = 1,
    calculator: NseOptionCostCalculator,
) -> list[SimulatedTrade]:
    snapshots = day.snapshots
    bars = _resample(candles, params.bar_minutes)
    if len(snapshots) < 5 or len(bars) <= params.range_lookback_bars:
        return []
    quantity = lot_size * lots
    entry_start = _parse_time(params.entry_start)
    entry_end = _parse_time(params.entry_end)
    squareoff = _parse_time(params.squareoff_time)
    trades: list[SimulatedTrade] = []
    position: _OpenPosition | None = None
    pending: _PendingBreakout | None = None
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

    def open_position(side: str, signal_time: datetime) -> bool:
        """Fill after the closed signal bar and require archived premium confirmation."""
        nonlocal position
        snapshot = snapshot_at_or_after(
            snapshots, signal_time, tolerance_seconds=params.fill_tolerance_seconds
        )
        if snapshot is None:
            return False
        setup = select_and_confirm(
            snapshots=snapshots, snapshot=snapshot,
            candles=candles,
            side=side, min_premium=params.min_premium, max_premium=params.max_premium,
            max_spread_pct=params.max_spread_pct, min_delta=params.minimum_option_delta,
            max_delta=params.maximum_option_delta,
            require_confirmation=params.require_option_momentum_confirmation,
        ) if params.use_index_option_context else None
        strike = setup.strike if setup else snapshot.atm_strike(side)
        quote = snapshot.quote(strike, side) if strike is not None else None
        if (
            quote is None or quote.ask <= 0 or quote.bid <= 0
            or quote.ltp < params.min_premium or quote.ask > params.max_premium
            or quote.spread_pct is None or quote.spread_pct > params.max_spread_pct
        ):
            return False
        if params.require_option_momentum_confirmation:
            earlier = next((item for item in reversed(snapshots) if item.timestamp < snapshot.timestamp), None)
            previous_quote = earlier.quote(strike, side) if earlier is not None else None
            if previous_quote is None or previous_quote.ltp <= 0:
                return False
            momentum = ((quote.ltp / previous_quote.ltp) - 1) * 100
            if momentum < params.option_momentum_minimum_pct:
                return False
        position = _OpenPosition(
            side=side, strike=strike, entry_price=setup.entry_price if setup else quote.ask, entry_snapshot=snapshot,
            stop_loss=setup.stop_loss if setup else round(quote.ask * (1 - params.stop_loss_pct / 100), 2),
            target=setup.target if setup else round(quote.ask * (1 + params.target_pct / 100), 2),
        )
        return True

    for index, bar in enumerate(bars):
        manage(bar.close_time)
        if position is not None:
            continue
        if pending is not None and index - pending.detected_at > params.retest_bars:
            pending = None
        if len(trades) >= params.max_trades_per_day or not entry_start <= bar.close_time.time() <= entry_end:
            continue
        if last_exit_at is not None and bar.close_time - last_exit_at < timedelta(minutes=params.cooldown_minutes):
            continue
        if params.pattern == "three-bar":
            side = _three_bar_signal(bars, index, params)
            if side is not None:
                open_position(side, bar.close_time)
            continue
        if pending is not None and index > pending.detected_at and _confirmation(bar, pending, params):
            if open_position(pending.side, bar.close_time):
                pending = None
                continue
        candidate = _new_breakout(bars, index, params)
        if candidate is not None:
            pending = candidate

    if position is not None:
        for snapshot in reversed(snapshots):
            quote = snapshot.quote(position.strike, position.side)
            if quote is not None and quote.bid > 0:
                close(snapshot, quote.bid, "DATA_END")
                break
    return trades
