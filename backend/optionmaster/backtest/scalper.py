"""Minute-level option-buying scalp strategy for stored Dhan chain snapshots.

The stored data is one chain snapshot roughly every 25–60 seconds, so this
strategy is written against that cadence rather than the tick stream the live
scalping engine uses. The rules are deliberately simple and fully deterministic:

Entry (momentum burst with premium confirmation):
- Spot moved at least ``momentum_threshold_pct`` over the last
  ``momentum_lookback_minutes`` — direction picks CE (up) or PE (down).
- The ATM option of that side also gained over the same window and traded
  fresh volume, so the premium is participating, not lagging.
- The quote is liquid: bid/ask present, spread below ``max_spread_pct``,
  premium at least ``min_premium`` rupees.
- Inside the entry window, under the daily trade cap, past the re-entry
  cooldown, and only one open position per symbol at a time.

Exit (first hit wins, checked on every later snapshot against the bid):
- Stop-loss at ``stop_loss_pct`` below entry.
- Target at ``target_pct`` above entry.
- Time stop after ``max_hold_minutes``.
- Forced square-off at ``squareoff_time`` IST.

Fills are conservative: buy at the stored ask, sell at the stored bid, and
every trade is charged the full NSE retail cost schedule.
"""

from datetime import datetime, time, timedelta

from pydantic import BaseModel, Field

from optionmaster.backtest.data import ChainSnapshot, StoredDay
from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.strategy.option_context import select_and_confirm


class ScalpParams(BaseModel):
    strategy_id: str = Field(default="stored-scalp-v1", pattern=r"^[a-z0-9-]{3,64}$")
    momentum_lookback_minutes: float = Field(default=3.0, ge=1.0, le=15.0)
    momentum_threshold_pct: float = Field(default=0.16, gt=0, le=1.0)
    stop_loss_pct: float = Field(default=10.0, gt=0, le=50.0)
    target_pct: float = Field(default=15.0, gt=0, le=100.0)
    max_hold_minutes: float = Field(default=12.0, ge=1.0, le=120.0)
    entry_start: str = Field(default="10:00", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field(default="13:00", pattern=r"^\d{2}:\d{2}$")
    squareoff_time: str = Field(default="15:12", pattern=r"^\d{2}:\d{2}$")
    max_spread_pct: float = Field(default=1.0, gt=0, le=5.0)
    min_premium: float = Field(default=20.0, ge=0)
    max_premium: float = Field(
        default=600.0,
        gt=0,
        description=(
            "Skip contracts richer than this. A percentage scalp target needs a "
            "gamma-rich premium; heavy monthly premiums move too little in percent terms."
        ),
    )
    cooldown_minutes: float = Field(default=5.0, ge=0, le=60.0)
    max_trades_per_day: int = Field(default=6, ge=1, le=50)
    require_premium_confirmation: bool = True
    use_index_option_context: bool = True
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)


class SimulatedTrade(BaseModel):
    symbol: str
    trade_date: str
    expiry: str
    side: str
    strike: float
    quantity: int
    lot_size: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_minutes: float
    spot_entry: float
    spot_exit: float
    gross_pnl: float
    charges: float
    net_pnl: float


def _parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


class _OpenPosition:
    __slots__ = ("side", "strike", "entry_price", "entry_snapshot", "stop_loss", "target")

    def __init__(self, *, side: str, strike: float, entry_price: float, entry_snapshot: ChainSnapshot, stop_loss: float, target: float) -> None:
        self.side = side
        self.strike = strike
        self.entry_price = entry_price
        self.entry_snapshot = entry_snapshot
        self.stop_loss = stop_loss
        self.target = target


def simulate_day(
    day: StoredDay,
    params: ScalpParams,
    *,
    candles=None,
    lot_size: int,
    lots: int = 1,
    calculator: NseOptionCostCalculator,
) -> list[SimulatedTrade]:
    """Replay one stored symbol-day through the scalp rules."""
    snapshots = day.snapshots
    if len(snapshots) < 5:
        return []
    quantity = lot_size * lots
    entry_start = _parse_time(params.entry_start)
    entry_end = _parse_time(params.entry_end)
    squareoff = _parse_time(params.squareoff_time)
    lookback = timedelta(minutes=params.momentum_lookback_minutes)
    trades: list[SimulatedTrade] = []
    position: _OpenPosition | None = None
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

    for index, snapshot in enumerate(snapshots):
        now_ist = snapshot.timestamp.time()

        if position is not None:
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
            elif now_ist >= squareoff:
                if bid is not None:
                    close(snapshot, bid, "SQUARE_OFF")
            continue

        if len(trades) >= params.max_trades_per_day:
            break
        if not entry_start <= now_ist <= entry_end:
            continue
        if last_exit_at is not None and snapshot.timestamp - last_exit_at < timedelta(
            minutes=params.cooldown_minutes
        ):
            continue

        reference = _reference_snapshot(snapshots, index, lookback)
        if reference is None or reference.spot <= 0:
            continue
        momentum_pct = ((snapshot.spot / reference.spot) - 1) * 100
        if abs(momentum_pct) < params.momentum_threshold_pct:
            continue
        side = "CE" if momentum_pct > 0 else "PE"
        setup = select_and_confirm(
            snapshots=snapshots, snapshot=snapshot,
            candles=candles or [],
            side=side, min_premium=params.min_premium, max_premium=params.max_premium,
            max_spread_pct=params.max_spread_pct, min_delta=params.minimum_option_delta,
            max_delta=params.maximum_option_delta, target_r=1.5,
            require_confirmation=params.require_premium_confirmation,
        ) if params.use_index_option_context and candles else None
        strike = setup.strike if setup else snapshot.atm_strike(side)
        if strike is None:
            continue
        quote = snapshot.quote(strike, side)
        if quote is None or quote.bid <= 0 or quote.ask <= 0 or quote.ltp < params.min_premium:
            continue
        if quote.ask > params.max_premium:
            continue
        spread = quote.spread_pct
        if spread is None or spread > params.max_spread_pct:
            continue
        if params.require_premium_confirmation:
            earlier = reference.quote(strike, side)
            if earlier is None or earlier.ltp <= 0:
                continue
            if quote.ltp <= earlier.ltp or quote.volume <= earlier.volume:
                continue
        entry_price = setup.entry_price if setup else quote.ask
        position = _OpenPosition(
            side=side,
            strike=strike,
            entry_price=entry_price,
            entry_snapshot=snapshot,
            stop_loss=setup.stop_loss if setup else round(entry_price * (1 - params.stop_loss_pct / 100), 2),
            target=setup.target if setup else round(entry_price * (1 + params.target_pct / 100), 2),
        )

    if position is not None:
        # Data ended with a position still open; close it on the last usable bid.
        for snapshot in reversed(snapshots):
            quote = snapshot.quote(position.strike, position.side)
            if quote is not None and quote.bid > 0:
                close(snapshot, quote.bid, "DATA_END")
                break
        position = None
    return trades


def _reference_snapshot(
    snapshots: list[ChainSnapshot], index: int, lookback: timedelta
) -> ChainSnapshot | None:
    """Find the most recent snapshot at least ``lookback`` before ``snapshots[index]``."""
    target = snapshots[index].timestamp - lookback
    for candidate in range(index - 1, -1, -1):
        if snapshots[candidate].timestamp <= target:
            return snapshots[candidate]
    return None
