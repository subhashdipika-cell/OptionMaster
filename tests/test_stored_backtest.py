from datetime import date

from optionmaster.backtest.data import StoredDataRepository
from optionmaster.backtest.runner import BacktestRunRequest, run_backtest, stored_data_status
from optionmaster.backtest.scalper import ScalpParams, simulate_day
from optionmaster.costs.calculator import NseOptionCostCalculator

HEADER = "time,underlying,under_ltp,expiry,strike,type,ltp,oi,prev_oi,iv,volume,delta,theta,vega,bid,ask\n"


def _row(time_utc: str, spot: float, strike: float, side: str, ltp: float, volume: float) -> str:
    bid = round(ltp - 0.5, 2)
    ask = round(ltp + 0.5, 2)
    return (
        f"{time_utc},NIFTY50,{spot},2026-07-21,{strike},{side},{ltp},1000,900,12.0,"
        f"{volume},0.5,-5.0,3.0,{bid},{ask}\n"
    )


def _write_day(directory, rows: list[str]) -> None:
    (directory / "NIFTY50_OPT_2026-07-14.csv").write_text(HEADER + "".join(rows), encoding="utf-8")


def test_momentum_burst_hits_target(tmp_path):
    # 05:00 UTC == 10:30 IST, inside the default entry window.
    rows = []
    spot = 24000.0
    premium = 100.0
    # Flat warm-up so the lookback window exists.
    for minute in range(0, 4):
        rows.append(_row(f"2026-07-14 05:0{minute}:00", spot, 24000, "CE", premium, 1000 + minute))
        rows.append(_row(f"2026-07-14 05:0{minute}:00", spot, 24000, "PE", premium, 1000 + minute))
    # Burst: spot jumps 0.2% and the CE premium participates with fresh volume.
    rows.append(_row("2026-07-14 05:04:00", 24048.0, 24000, "CE", 112.0, 2000))
    rows.append(_row("2026-07-14 05:04:00", 24048.0, 24000, "PE", 88.0, 1200))
    # Target: entry was at the ask (112.5); +15% target = 129.38, bid here is 131.5.
    rows.append(_row("2026-07-14 05:06:00", 24090.0, 24000, "CE", 132.0, 2400))
    rows.append(_row("2026-07-14 05:06:00", 24090.0, 24000, "PE", 70.0, 1300))
    _write_day(tmp_path, rows)

    repository = StoredDataRepository(tmp_path)
    stored = repository.load_day("NIFTY50", date(2026, 7, 14))
    trades = simulate_day(
        stored,
        ScalpParams(momentum_threshold_pct=0.16, max_spread_pct=2.0),
        lot_size=75,
        calculator=NseOptionCostCalculator(),
    )
    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == "CE"
    assert trade.exit_reason == "TARGET"
    assert trade.entry_price == 112.5
    assert trade.exit_price == 131.5
    assert trade.net_pnl < trade.gross_pnl  # charges must always be deducted
    assert trade.gross_pnl == round((131.5 - 112.5) * 75, 2)


def test_no_entry_when_spot_is_flat(tmp_path):
    rows = []
    for minute in range(0, 10):
        rows.append(_row(f"2026-07-14 05:{minute:02d}:00", 24000.0, 24000, "CE", 100.0, 1000))
    _write_day(tmp_path, rows)
    repository = StoredDataRepository(tmp_path)
    stored = repository.load_day("NIFTY50", date(2026, 7, 14))
    trades = simulate_day(
        stored, ScalpParams(), lot_size=75, calculator=NseOptionCostCalculator()
    )
    assert trades == []


def test_run_backtest_aggregates_and_status(tmp_path):
    rows = []
    for minute in range(0, 4):
        rows.append(_row(f"2026-07-14 05:0{minute}:00", 24000.0, 24000, "CE", 100.0, 1000 + minute))
    rows.append(_row("2026-07-14 05:04:00", 24048.0, 24000, "CE", 112.0, 2000))
    rows.append(_row("2026-07-14 05:06:00", 24090.0, 24000, "CE", 132.0, 2400))
    _write_day(tmp_path, rows)

    repository = StoredDataRepository(tmp_path)
    status = stored_data_status(repository)
    assert status.directory_available
    assert status.symbols == {"NIFTY50": 1}

    run = run_backtest(
        BacktestRunRequest(params=ScalpParams(max_spread_pct=2.0)),
        repository=repository,
        calculator=NseOptionCostCalculator(),
        lot_sizes={"NIFTY50": 75},
    )
    assert run.summary.trades == 1
    assert run.summary.days_tested == 1
    assert run.by_symbol[0].label == "NIFTY50"
    assert run.trades[0].exit_reason == "TARGET"


def test_symbols_without_lot_size_are_skipped(tmp_path):
    _write_day(tmp_path, [_row("2026-07-14 05:00:00", 24000.0, 24000, "CE", 100.0, 1000)])
    run = run_backtest(
        BacktestRunRequest(),
        repository=StoredDataRepository(tmp_path),
        calculator=NseOptionCostCalculator(),
        lot_sizes={},
    )
    assert run.summary.trades == 0
    assert run.summary.days_tested == 0
