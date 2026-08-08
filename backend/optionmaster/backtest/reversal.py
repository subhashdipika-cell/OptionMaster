"""Failed-breakdown ("liquidity sweep") reversal scalp on 1-minute index candles.

Where the momentum scalp in ``scalper.py`` buys continuation, this buys the
rejection of a failed move. The signal is read from real M1 spot OHLC, because a
sweep is defined by the intrabar extreme — the low that got taken out — which
the ~71s chain snapshots cannot see. Fills still come from the chain.

Entry (bullish; mirrored for bearish):
- Over the previous ``sweep_lookback_bars`` candles, note the lowest low L.
- The signal candle trades BELOW L (the sweep) but CLOSES back above it (the
  failure), and closes in the upper ``reclaim_pct`` of its own range.
- The sweep is meaningful: it dug at least ``min_sweep_pct`` of spot below L,
  so we skip one-tick nicks that are noise.
- Direction: a swept low → buy CE, a swept high → buy PE.
- The ATM quote of that side is liquid: bid/ask present, spread under
  ``max_spread_pct``, premium within [``min_premium``, ``max_premium``].
- Inside the entry window, under the daily cap, past the cooldown, flat.

Exit (first hit wins, checked on each later snapshot against the bid):
- Stop-loss ``stop_loss_pct`` below entry, target ``target_pct`` above.
- Spot invalidation: price closes back beyond the swept extreme, i.e. the
  reversal thesis failed. Reversal-specific, and usually faster than the
  premium stop.
- Time stop after ``max_hold_minutes``; forced square-off at ``squareoff_time``.

Fills are conservative: buy at the stored ask, sell at the stored bid, full NSE
retail cost schedule on every trade. Entry is taken at the first snapshot at or
after the signal candle CLOSES — never on the candle that produced the signal,
which would be lookahead.
"""

from datetime import datetime, time, timedelta

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.backtest.scalper import SimulatedTrade, _parse_time
from optionmaster.backtest.spot import SpotCandle, snapshot_at_or_after
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class ReversalParams(BaseModel):
    strategy_id: str = Field(default="stored-reversal-v1", pattern=r"^[a-z0-9-]{3,64}$")
    sweep_lookback_bars: int = Field(
        default=15, ge=3, le=120, description="Candles scanned for the level being swept."
    )
    min_sweep_pct: float = Field(
        default=0.02,
        ge=0,
        le=1.0,
        description="How far past the level the sweep must dig, as % of spot.",
    )
    reclaim_pct: float = Field(
        default=60.0,
        ge=0,
        le=100.0,
        description="Close must land in this top %% of the candle's range (bullish).",
    )
    stop_loss_pct: float = Field(default=10.0, gt=0, le=50.0)
    target_pct: float = Field(default=15.0, gt=0, le=100.0)
    max_hold_minutes: float = Field(
        default=12.0,
        ge=5.0,
        le=120.0,
        description=(
            "Floor of 5 is deliberate: the chain samples ~71s apart, so a shorter "
            "hold would be judged on 3-4 observations and mean very little."
        ),
    )
    entry_start: str = Field(default="09:30", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field(default="14:30", pattern=r"^\d{2}:\d{2}$")
    squareoff_time: str = Field(default="15:12", pattern=r"^\d{2}:\d{2}$")
    max_spread_pct: float = Field(default=1.0, gt=0, le=5.0)
    min_premium: float = Field(default=20.0, ge=0)
    max_premium: float = Field(default=600.0, gt=0)
    cooldown_minutes: float = Field(default=5.0, ge=0, le=60.0)
    max_trades_per_day: int = Field(default=6, ge=1, le=50)
    spot_invalidation: bool = Field(
        default=True,
        description="Exit when spot closes back beyond the swept extreme.",
    )
    fill_tolerance_seconds: float = Field(
        default=150.0,
        ge=60.0,
        le=600.0,
        description=(
            "Skip the signal if the next chain snapshot is further away than this. "
            "Guards against pretending we could trade across a data hole."
        ),
    )
    use_index_option_context: bool = True
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)


class _OpenPosition:
    __slots__ = (
        "side", "strike", "entry_price", "entry_snapshot",
        "stop_loss", "target", "invalidation_level",
    )

    def __init__(
        self, *, side: str, strike: float, entry_price: float,
        entry_snapshot: ChainSnapshot, stop_loss: float, target: float,
        invalidation_level: float,
    ) -> None:
        self.side = side
        self.strike = strike
        self.entry_price = entry_price
        self.entry_snapshot = entry_snapshot
        self.stop_loss = stop_loss
        self.target = target
        self.invalidation_level = invalidation_level


def _sweep_signal(
    candles: list[SpotCandle], index: int, params: ReversalParams
) -> tuple[str, float] | None:
    """Return ``(side, invalidation_level)`` if candle ``index`` is a failed breakout.

    The invalidation level is the SWEEP EXTREME (the signal candle's own low for a
    bullish sweep), not the level that was reclaimed. Price oscillates around the
    reclaimed level for several minutes after a sweep, so using it as a stop exits
    on noise; the extreme is what has to hold for the reversal to still be valid.
    """
    start = index - params.sweep_lookback_bars
    if start < 0:
        return None
    window = candles[start:index]
    if not window:
        return None
    candle = candles[index]
    if candle.range <= 0:
        return None

    prior_low = min(item.low for item in window)
    prior_high = max(item.high for item in window)
    threshold = candle.close * (params.min_sweep_pct / 100)

    # Bullish: dug below the prior low, closed back above it, closed strong.
    if candle.low < prior_low - threshold and candle.close > prior_low:
        position_in_range = (candle.close - candle.low) / candle.range * 100
        if position_in_range >= params.reclaim_pct:
            return "CE", candle.low

    # Bearish: poked above the prior high, closed back under it, closed weak.
    if candle.high > prior_high + threshold and candle.close < prior_high:
        position_in_range = (candle.high - candle.close) / candle.range * 100
        if position_in_range >= params.reclaim_pct:
            return "PE", candle.high

    return None


def simulate_day(
    day: StoredDay,
    candles: list[SpotCandle],
    params: ReversalParams,
    *,
    lot_size: int,
    lots: int = 1,
    calculator: NseOptionCostCalculator,
) -> list[SimulatedTrade]:
    """Replay one stored symbol-day through the sweep-reversal rules."""
    snapshots = day.snapshots
    if len(snapshots) < 5 or len(candles) <= params.sweep_lookback_bars:
        return []

    quantity = lot_size * lots
    entry_start = _parse_time(params.entry_start)
    entry_end = _parse_time(params.entry_end)
    squareoff = _parse_time(params.squareoff_time)
    trades: list[SimulatedTrade] = []
    position: _OpenPosition | None = None
    entry_candle_index = 0
    last_exit_at: datetime | None = None

    def close(current: ChainSnapshot, price: float, reason: str) -> None:
        nonlocal position, last_exit_at
        assert position is not None
        result = calculator.net_result(
            entry_price=position.entry_price, exit_price=price, quantity=quantity,
            underlying=day.symbol,
        )
        trades.append(
            SimulatedTrade(
                symbol=day.symbol,
                trade_date=day.day.isoformat(),
                expiry=current.expiry,
                side=position.side,
                strike=position.strike,
                quantity=quantity,
                lot_size=lot_size,
                entry_time=position.entry_snapshot.timestamp,
                exit_time=current.timestamp,
                entry_price=position.entry_price,
                exit_price=price,
                exit_reason=reason,
                hold_minutes=round(
                    (current.timestamp - position.entry_snapshot.timestamp).total_seconds() / 60, 2
                ),
                spot_entry=position.entry_snapshot.spot,
                spot_exit=current.spot,
                gross_pnl=result.gross_pnl,
                charges=result.costs.total,
                net_pnl=result.net_pnl,
            )
        )
        last_exit_at = current.timestamp
        position = None

    def manage(upto: datetime) -> None:
        """Walk the chain snapshots up to ``upto`` looking for an exit."""
        nonlocal position
        if position is None:
            return
        for snapshot in snapshots:
            if position is None:
                break
            if snapshot.timestamp <= position.entry_snapshot.timestamp:
                continue
            if snapshot.timestamp > upto:
                break
            quote = snapshot.quote(position.strike, position.side)
            bid = quote.bid if quote is not None and quote.bid > 0 else None
            held = snapshot.timestamp - position.entry_snapshot.timestamp
            if bid is not None and bid <= position.stop_loss:
                close(snapshot, bid, "STOP_LOSS")
            elif bid is not None and bid >= position.target:
                close(snapshot, bid, "TARGET")
            elif held >= timedelta(minutes=params.max_hold_minutes):
                if bid is not None:
                    close(snapshot, bid, "TIME_STOP")
            elif snapshot.timestamp.time() >= squareoff:
                if bid is not None:
                    close(snapshot, bid, "SQUARE_OFF")

    for index, candle in enumerate(candles):
        closed_at = candle.close_time

        # Manage an open position on every snapshot up to this candle's close.
        manage(closed_at)

        if position is not None:
            # Reversal thesis broken: spot closed back through the swept level.
            if params.spot_invalidation and index > entry_candle_index:
                failed = (
                    candle.close < position.invalidation_level
                    if position.side == "CE"
                    else candle.close > position.invalidation_level
                )
                if failed:
                    exit_snapshot = snapshot_at_or_after(
                        snapshots, closed_at,
                        tolerance_seconds=params.fill_tolerance_seconds,
                    )
                    if exit_snapshot is not None:
                        quote = exit_snapshot.quote(position.strike, position.side)
                        if quote is not None and quote.bid > 0:
                            close(exit_snapshot, quote.bid, "SPOT_INVALIDATION")
            continue

        if len(trades) >= params.max_trades_per_day:
            break
        now_ist = closed_at.time()
        if not entry_start <= now_ist <= entry_end:
            continue
        if last_exit_at is not None and closed_at - last_exit_at < timedelta(
            minutes=params.cooldown_minutes
        ):
            continue

        signal = _sweep_signal(candles, index, params)
        if signal is None:
            continue
        side, level = signal

        # Fill at the first chain snapshot AFTER the signal candle closed.
        snapshot = snapshot_at_or_after(
            snapshots, closed_at, tolerance_seconds=params.fill_tolerance_seconds
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
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            continue
        if quote.ltp < params.min_premium or quote.ask > params.max_premium:
            continue
        spread = quote.spread_pct
        if spread is None or spread > params.max_spread_pct:
            continue

        entry_price = setup.entry_price if setup else quote.ask
        position = _OpenPosition(
            side=side,
            strike=strike,
            entry_price=entry_price,
            entry_snapshot=snapshot,
            stop_loss=setup.stop_loss if setup else round(entry_price * (1 - params.stop_loss_pct / 100), 2),
            target=setup.target if setup else round(entry_price * (1 + params.target_pct / 100), 2),
            invalidation_level=level,
        )
        entry_candle_index = index

    if position is not None:
        manage(snapshots[-1].timestamp)
    if position is not None:
        for snapshot in reversed(snapshots):
            quote = snapshot.quote(position.strike, position.side)
            if quote is not None and quote.bid > 0:
                close(snapshot, quote.bid, "DATA_END")
                break
        position = None
    return trades
