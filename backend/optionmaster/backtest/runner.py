"""Runs the stored-data backtests across symbols and days and aggregates results.

The runner supports research replays selected by ``BacktestRunRequest.strategy``:
stored momentum, failed-breakdown reversal, and opening-range/VWAP.  Only the
first uses chain snapshots alone; the latter two use M1 spot candles for the
signal and archived option quotes for conservative fills.
"""

from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from optionmaster.backtest.data import StoredDataRepository
from optionmaster.backtest.orb_vwap import OrbVwapParams
from optionmaster.backtest.orb_vwap import simulate_day as simulate_orb_vwap_day
from optionmaster.backtest.reversal import ReversalParams
from optionmaster.backtest.reversal import simulate_day as simulate_reversal_day
from optionmaster.backtest.scalper import ScalpParams, SimulatedTrade, simulate_day
from optionmaster.backtest.spot import SpotCandleRepository
from optionmaster.costs.calculator import NseOptionCostCalculator

StrategyName = Literal["stored-scalp-v1", "stored-reversal-v1", "stored-orb-vwap-v1"]


class BacktestRunRequest(BaseModel):
    symbols: list[str] | None = Field(
        default=None, description="Underlyings to include; default is every stored symbol."
    )
    start_date: date | None = None
    end_date: date | None = None
    lots: int = Field(default=1, ge=1, le=10)
    # `strategy` selects which param block below is used. Both are kept as
    # separate optional fields rather than a discriminated union so that runs
    # persisted before the reversal strategy existed still deserialize.
    strategy: StrategyName = "stored-scalp-v1"
    params: ScalpParams = Field(default_factory=ScalpParams)
    reversal: ReversalParams = Field(default_factory=ReversalParams)
    orb_vwap: OrbVwapParams = Field(default_factory=OrbVwapParams)

    @property
    def strategy_id(self) -> str:
        if self.strategy == "stored-reversal-v1":
            return self.reversal.strategy_id
        if self.strategy == "stored-orb-vwap-v1":
            return self.orb_vwap.strategy_id
        return self.params.strategy_id


class BreakdownBucket(BaseModel):
    label: str
    trades: int
    wins: int
    net_pnl: float


class BacktestRunSummary(BaseModel):
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_pnl: float
    charges: float
    net_pnl: float
    profit_factor: float | None = None
    max_drawdown: float
    average_hold_minutes: float
    average_win: float
    average_loss: float
    days_tested: int
    first_day: str | None = None
    last_day: str | None = None


class BacktestRun(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    strategy_id: str
    request: BacktestRunRequest
    summary: BacktestRunSummary
    by_symbol: list[BreakdownBucket]
    by_hour: list[BreakdownBucket]
    by_exit_reason: list[BreakdownBucket]
    trades: list[SimulatedTrade]


class BacktestRunOverview(BaseModel):
    """Listing row: a run without its full trade payload."""

    id: str
    created_at: datetime
    strategy_id: str
    summary: BacktestRunSummary


class StoredDataStatus(BaseModel):
    directory_available: bool
    symbols: dict[str, int]
    first_day: str | None = None
    last_day: str | None = None
    total_symbol_days: int


def stored_data_status(repository: StoredDataRepository) -> StoredDataStatus:
    days = repository.list_days()
    symbols: dict[str, int] = {}
    for symbol, _ in days:
        symbols[symbol] = symbols.get(symbol, 0) + 1
    dates = sorted({item.isoformat() for _, item in days})
    return StoredDataStatus(
        directory_available=repository.available,
        symbols=symbols,
        first_day=dates[0] if dates else None,
        last_day=dates[-1] if dates else None,
        total_symbol_days=len(days),
    )


def run_backtest(
    request: BacktestRunRequest,
    *,
    repository: StoredDataRepository,
    calculator: NseOptionCostCalculator,
    lot_sizes: dict[str, int],
    spot_repository: SpotCandleRepository | None = None,
) -> BacktestRun:
    wanted = {symbol.upper() for symbol in request.symbols} if request.symbols else None
    needs_spot = request.strategy in {"stored-reversal-v1", "stored-orb-vwap-v1"}
    if needs_spot and spot_repository is None:
        raise ValueError(f"{request.strategy} needs a SpotCandleRepository (M1 index candles).")
    trades: list[SimulatedTrade] = []
    tested_days = 0
    for symbol, day in repository.list_days():
        if wanted is not None and symbol not in wanted:
            continue
        if symbol not in lot_sizes:
            continue
        if request.start_date and day < request.start_date:
            continue
        if request.end_date and day > request.end_date:
            continue
        stored = repository.load_day(symbol, day)
        if stored is None or not stored.snapshots:
            continue
        if needs_spot:
            assert spot_repository is not None
            candles = spot_repository.load_day(symbol, day)
            if not candles:
                # No M1 candles for this symbol-day (e.g. FINNIFTY): the reversal
                # signal is undefined, so the day is skipped rather than counted.
                continue
            tested_days += 1
            if request.strategy == "stored-reversal-v1":
                trades.extend(simulate_reversal_day(stored, candles, request.reversal, lot_size=lot_sizes[symbol], lots=request.lots, calculator=calculator))
            else:
                trades.extend(simulate_orb_vwap_day(stored, candles, request.orb_vwap, lot_size=lot_sizes[symbol], lots=request.lots, calculator=calculator))
            continue
        tested_days += 1
        candles = spot_repository.load_day(symbol, day) if spot_repository is not None else None
        trades.extend(
            simulate_day(
                stored,
                request.params,
                candles=candles,
                lot_size=lot_sizes[symbol],
                lots=request.lots,
                calculator=calculator,
            )
        )
    trades.sort(key=lambda trade: trade.entry_time)
    return BacktestRun(
        strategy_id=request.strategy_id,
        request=request,
        summary=_summarize(trades, tested_days),
        by_symbol=_bucket(trades, lambda trade: trade.symbol),
        by_hour=_bucket(trades, lambda trade: f"{trade.entry_time.hour:02d}:00"),
        by_exit_reason=_bucket(trades, lambda trade: trade.exit_reason),
        trades=trades,
    )


def _summarize(trades: list[SimulatedTrade], tested_days: int) -> BacktestRunSummary:
    wins = [trade for trade in trades if trade.net_pnl > 0]
    losses = [trade for trade in trades if trade.net_pnl < 0]
    profit = sum(trade.net_pnl for trade in wins)
    loss = abs(sum(trade.net_pnl for trade in losses))
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        running += trade.net_pnl
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    days = sorted({trade.trade_date for trade in trades})
    return BacktestRunSummary(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
        gross_pnl=round(sum(trade.gross_pnl for trade in trades), 2),
        charges=round(sum(trade.charges for trade in trades), 2),
        net_pnl=round(sum(trade.net_pnl for trade in trades), 2),
        profit_factor=round(profit / loss, 4) if loss else None,
        max_drawdown=round(max_drawdown, 2),
        average_hold_minutes=round(
            sum(trade.hold_minutes for trade in trades) / len(trades), 2
        )
        if trades
        else 0.0,
        average_win=round(profit / len(wins), 2) if wins else 0.0,
        average_loss=round(-loss / len(losses), 2) if losses else 0.0,
        days_tested=tested_days,
        first_day=days[0] if days else None,
        last_day=days[-1] if days else None,
    )


def _bucket(trades: list[SimulatedTrade], key) -> list[BreakdownBucket]:
    grouped: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        grouped.setdefault(key(trade), []).append(trade)
    return [
        BreakdownBucket(
            label=label,
            trades=len(items),
            wins=sum(trade.net_pnl > 0 for trade in items),
            net_pnl=round(sum(trade.net_pnl for trade in items), 2),
        )
        for label, items in sorted(grouped.items())
    ]
