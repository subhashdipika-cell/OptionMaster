from datetime import datetime, timedelta, timezone

from optionmaster.backtest.orb_vwap import _directional_movement, _session_vwap
from optionmaster.backtest.spot import SpotCandle
from optionmaster.context.models import PriceBar
from optionmaster.market.models import OptionSide
from optionmaster.strategy.orb_vwap_live import diagnose_m1_bars, evaluate_m1_bars


IST = timezone(timedelta(hours=5, minutes=30))


def test_directional_system_confirms_a_sustained_uptrend_without_future_bars():
    candles = []
    start = datetime(2026, 7, 14, 9, 15, tzinfo=IST)
    for index in range(40):
        price = 24000.0 + index * 4
        candles.append(SpotCandle(
            timestamp=start + timedelta(minutes=index), open=price, high=price + 3,
            low=price - 1, close=price + 2, volume=100 + index,
        ))

    vwap = _session_vwap(candles)
    plus_di, minus_di, adx = _directional_movement(candles, period=14)

    assert vwap[-1] is not None and vwap[-1] < candles[-1].close
    assert plus_di[-1] is not None and minus_di[-1] is not None and plus_di[-1] > minus_di[-1]
    assert adx[-1] is not None and adx[-1] >= 20


def test_live_evaluator_uses_only_completed_m1_bars_for_a_bullish_orb_setup():
    start = datetime(2026, 7, 14, 9, 15, tzinfo=IST)
    bars = []
    for index in range(51):
        price = 24000.0 if index < 15 else 24000.0 + (index - 14) * 8
        bars.append(PriceBar(
            timestamp=start + timedelta(minutes=index), open=price, high=price + 6,
            low=price - 2, close=price + 5,
            volume=1000 if index >= 45 else 100,
        ))

    setup = evaluate_m1_bars(bars, now=start + timedelta(minutes=51))

    assert setup is not None
    assert setup.side is OptionSide.CE
    assert setup.signal_bar_closed_at == start + timedelta(minutes=50)


def test_live_diagnostic_names_the_missing_opening_range_breakout():
    start = datetime(2026, 7, 14, 9, 15, tzinfo=IST)
    bars = [
        PriceBar(
            timestamp=start + timedelta(minutes=index), open=24000, high=24005,
            low=23995, close=24000, volume=100,
        )
        for index in range(41)
    ]

    diagnostic = diagnose_m1_bars(bars, now=start + timedelta(minutes=41))
    breakout = next(gate for gate in diagnostic.gates if gate.key == "breakout")

    assert diagnostic.ready is False
    assert breakout.passed is False
    assert "remains inside" in breakout.detail
