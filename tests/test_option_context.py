from datetime import datetime, timedelta, timezone

from optionmaster.backtest.data import ChainQuote, ChainSnapshot
from optionmaster.backtest.spot import SpotCandle, IST
from optionmaster.strategy.option_context import select_and_confirm


def test_selected_option_requires_index_and_premium_confirmation():
    start = datetime(2026, 7, 14, 9, 15, tzinfo=IST)
    candles = [
        SpotCandle(start + timedelta(minutes=i), 24000 + i, 24002 + i, 23998 + i, 24001 + i, 100)
        for i in range(25)
    ]
    snapshots = []
    premiums = [100, 101, 102, 103, 110]
    for i, premium in enumerate(premiums):
        snapshot = ChainSnapshot(start + timedelta(minutes=20 + i), 24020 + i, "2026-07-21")
        snapshot.quotes[(24000.0, "CE")] = ChainQuote(
            strike=24000, side="CE", ltp=premium, bid=premium - 1, ask=premium + 1,
            oi=1000, prev_oi=900, iv=12, volume=2000, delta=0.5,
        )
        snapshots.append(snapshot)
    setup = select_and_confirm(
        snapshots=snapshots, snapshot=snapshots[-1], candles=candles, side="CE",
        min_premium=20, max_premium=600, max_spread_pct=2.0,
    )
    assert setup is not None
    assert setup.strike == 24000
    assert setup.stop_loss < setup.entry_price < setup.target


def test_selected_option_stands_down_without_index_chart_history():
    snapshot = ChainSnapshot(datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc), 24000, "2026-07-21")
    snapshot.quotes[(24000.0, "CE")] = ChainQuote(
        strike=24000, side="CE", ltp=100, bid=99, ask=101,
        oi=1000, prev_oi=900, iv=12, volume=2000, delta=0.5,
    )
    assert select_and_confirm(
        snapshots=[snapshot], snapshot=snapshot, candles=[], side="CE",
        min_premium=20, max_premium=600, max_spread_pct=2.0,
    ) is None
