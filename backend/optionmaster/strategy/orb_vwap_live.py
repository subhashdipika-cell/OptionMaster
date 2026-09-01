"""Live, paper-trial evaluator for the fixed ORB/VWAP research candidate.

It shares the replay definition: M1 bars build session VWAP and Wilder
DMI/ADX, while complete five-minute bars define the opening range and the
breakout. It only returns a setup; it has no Dhan order functionality.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from optionmaster.backtest.orb_vwap import OrbVwapParams, _directional_movement, _session_vwap
from optionmaster.backtest.pullback import _resample
from optionmaster.backtest.spot import IST, SpotCandle
from optionmaster.context.models import PriceBar
from optionmaster.market.models import OptionSide


@dataclass(frozen=True, slots=True)
class OrbVwapSetup:
    side: OptionSide
    opening_high: float
    opening_low: float
    vwap: float
    adx: float
    signal_bar_closed_at: datetime


class OrbVwapGate(BaseModel):
    """One visible condition in the live ORB/VWAP checklist."""

    key: str
    label: str
    passed: bool | None = None
    detail: str


class OrbVwapDiagnostics(BaseModel):
    """Read-only explanation of the current structural ORB/VWAP state."""

    timestamp: datetime
    phase: str
    ready: bool = False
    candidate_side: OptionSide | None = None
    latest_bar_closed_at: datetime | None = None
    opening_high: float | None = None
    opening_low: float | None = None
    latest_close: float | None = None
    vwap: float | None = None
    adx: float | None = None
    volume: float | None = None
    required_volume: float | None = None
    gates: list[OrbVwapGate] = Field(default_factory=list)
    summary: str


class PremiumMomentumTracker:
    """Keeps only the prior observed option LTP for paper-trial confirmation."""

    def __init__(self) -> None:
        self._prices: dict[tuple[float, OptionSide], float] = {}

    def observe_and_change(self, snapshot, *, strike: float, side: OptionSide) -> float | None:
        quote = next((item for item in snapshot.option_quotes if item.strike == strike and item.side is side), None)
        prior = self._prices.get((strike, side))
        self.observe(snapshot)
        if quote is None or quote.ltp <= 0 or prior is None or prior <= 0:
            return None
        return ((quote.ltp / prior) - 1) * 100

    def latest_change(self, snapshot, *, strike: float, side: OptionSide) -> float | None:
        """Read the currently tracked premium change without changing state."""
        quote = next((item for item in snapshot.option_quotes if item.strike == strike and item.side is side), None)
        prior = self._prices.get((strike, side))
        if quote is None or quote.ltp <= 0 or prior is None or prior <= 0:
            return None
        return ((quote.ltp / prior) - 1) * 100

    def observe(self, snapshot) -> None:
        for item in snapshot.option_quotes:
            if item.ltp > 0:
                self._prices[(item.strike, item.side)] = item.ltp


def evaluate_m1_bars(
    bars: list[PriceBar], *, params: OrbVwapParams | None = None, now: datetime | None = None
) -> OrbVwapSetup | None:
    """Return the latest closed ORB setup from Dhan M1 bars, without lookahead."""
    params = params or OrbVwapParams()
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    candles = [
        SpotCandle(
            timestamp=bar.timestamp.astimezone(IST), open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume,
        )
        for bar in bars
        if bar.timestamp.astimezone(IST).date() == now_ist.date()
        and bar.timestamp.astimezone(IST) + timedelta(minutes=1) <= now_ist
    ]
    candles.sort(key=lambda item: item.timestamp)
    if len(candles) < params.adx_period * 2:
        return None
    five_minute = _resample(candles, params.bar_minutes)
    opening_bars = params.opening_range_minutes // params.bar_minutes
    if len(five_minute) <= opening_bars:
        return None
    bar = five_minute[-1]
    if bar.close_time > now_ist or not _within(bar.close_time, params.entry_start, params.entry_end):
        return None
    opening_high = max(item.high for item in five_minute[:opening_bars])
    opening_low = min(item.low for item in five_minute[:opening_bars])
    opening_volume = sum(item.volume for item in candles[:params.opening_range_minutes])
    if opening_volume <= 0:
        return None
    current_vwap = _session_vwap(candles)[-1]
    plus_di, minus_di, adx = _directional_movement(candles, params.adx_period)
    if current_vwap is None or plus_di[-1] is None or minus_di[-1] is None or adx[-1] is None:
        return None
    bar_volume = sum(item.volume for item in candles if bar.timestamp <= item.timestamp < bar.close_time)
    if bar_volume < (opening_volume / opening_bars) * params.volume_multiple:
        return None
    if bar.close > opening_high and bar.close > current_vwap and plus_di[-1] > minus_di[-1] and adx[-1] >= params.adx_minimum:
        return OrbVwapSetup(OptionSide.CE, opening_high, opening_low, current_vwap, adx[-1], bar.close_time)
    if bar.close < opening_low and bar.close < current_vwap and minus_di[-1] > plus_di[-1] and adx[-1] >= params.adx_minimum:
        return OrbVwapSetup(OptionSide.PE, opening_high, opening_low, current_vwap, adx[-1], bar.close_time)
    return None


def diagnose_m1_bars(
    bars: list[PriceBar], *, params: OrbVwapParams | None = None, now: datetime | None = None
) -> OrbVwapDiagnostics:
    """Explain every structural gate using only completed bars available now."""
    params = params or OrbVwapParams()
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    candles = _today_completed_candles(bars, now_ist)
    gates: list[OrbVwapGate] = []
    minimum_bars = params.adx_period * 2
    has_warmup = len(candles) >= minimum_bars
    gates.append(OrbVwapGate(
        key="warmup", label="Indicator warm-up", passed=has_warmup,
        detail=f"{len(candles)} completed M1 bars; need {minimum_bars}.",
    ))
    if not has_warmup:
        return OrbVwapDiagnostics(
            timestamp=now_ist, phase="WARMING_UP", gates=gates,
            summary="Waiting for enough completed one-minute bars to calculate ADX/DMI.",
        )
    five_minute = _resample(candles, params.bar_minutes)
    opening_bars = params.opening_range_minutes // params.bar_minutes
    has_opening_range = len(five_minute) > opening_bars
    gates.append(OrbVwapGate(
        key="opening_range", label="Opening range", passed=has_opening_range,
        detail=(f"{params.opening_range_minutes}-minute range is complete."
                if has_opening_range else "Waiting for the 09:15–09:30 range to complete."),
    ))
    if not has_opening_range:
        return OrbVwapDiagnostics(
            timestamp=now_ist, phase="OPENING_RANGE", gates=gates,
            summary="Waiting for the opening range to complete.",
        )
    bar = five_minute[-1]
    in_entry_window = _within(bar.close_time, params.entry_start, params.entry_end)
    gates.append(OrbVwapGate(
        key="entry_window", label="Entry window", passed=in_entry_window,
        detail=(f"{bar.close_time:%H:%M} IST; window {params.entry_start}–{params.entry_end} IST."
                if in_entry_window else f"Latest completed bar is outside the {params.entry_start}–{params.entry_end} IST entry window."),
    ))
    opening_high = max(item.high for item in five_minute[:opening_bars])
    opening_low = min(item.low for item in five_minute[:opening_bars])
    vwap = _session_vwap(candles)[-1]
    plus_di, minus_di, adx = _directional_movement(candles, params.adx_period)
    current_vwap, current_plus, current_minus, current_adx = vwap, plus_di[-1], minus_di[-1], adx[-1]
    upward_breakout = bar.close > opening_high
    downward_breakout = bar.close < opening_low
    breakout_side = OptionSide.CE if upward_breakout else OptionSide.PE if downward_breakout else None
    gates.append(OrbVwapGate(
        key="breakout", label="Opening-range breakout", passed=breakout_side is not None,
        detail=(f"CE: close {bar.close:.2f} above range high {opening_high:.2f}." if upward_breakout
                else f"PE: close {bar.close:.2f} below range low {opening_low:.2f}." if downward_breakout
                else f"Close {bar.close:.2f} remains inside {opening_low:.2f}–{opening_high:.2f}."),
    ))
    price_vwap_ok: bool | None = None if breakout_side is None else bool(
        (upward_breakout and current_vwap is not None and bar.close > current_vwap)
        or (downward_breakout and current_vwap is not None and bar.close < current_vwap)
    )
    gates.append(OrbVwapGate(
        key="vwap", label="VWAP alignment", passed=price_vwap_ok,
        detail=("Waiting for a breakout direction before applying the VWAP rule." if breakout_side is None
                else f"Close {bar.close:.2f}; session VWAP {current_vwap:.2f}." if current_vwap is not None
                else "VWAP is unavailable because volume data is incomplete."),
    ))
    trend_ok: bool | None = None if breakout_side is None else bool(
        (upward_breakout and current_plus is not None and current_minus is not None and current_adx is not None
         and current_plus > current_minus and current_adx >= params.adx_minimum)
        or (downward_breakout and current_plus is not None and current_minus is not None and current_adx is not None
         and current_minus > current_plus and current_adx >= params.adx_minimum)
    )
    gates.append(OrbVwapGate(
        key="trend", label="ADX / DMI trend", passed=trend_ok,
        detail=("Waiting for a breakout direction before applying the DMI direction rule." if breakout_side is None
                else f"ADX {current_adx:.1f} (need ≥ {params.adx_minimum:.0f}); +DI {current_plus:.1f}; −DI {current_minus:.1f}."
                if None not in (current_adx, current_plus, current_minus)
                else "ADX/DMI is still unavailable."),
    ))
    opening_volume = sum(item.volume for item in candles[:params.opening_range_minutes])
    current_volume = sum(item.volume for item in candles if bar.timestamp <= item.timestamp < bar.close_time)
    required_volume = (opening_volume / opening_bars) * params.volume_multiple if opening_volume > 0 else 0.0
    volume_ok = opening_volume > 0 and current_volume >= required_volume
    gates.append(OrbVwapGate(
        key="volume", label="Breakout volume", passed=volume_ok,
        detail=(f"{current_volume:.0f} on latest 5m bar; need ≥ {required_volume:.0f}."
                if opening_volume > 0 else "Opening-range volume is unavailable."),
    ))
    ready = in_entry_window and breakout_side is not None and price_vwap_ok and trend_ok and volume_ok
    return OrbVwapDiagnostics(
        timestamp=now_ist, phase="READY" if ready else "EVALUATING", ready=ready,
        candidate_side=breakout_side, latest_bar_closed_at=bar.close_time,
        opening_high=opening_high, opening_low=opening_low, latest_close=bar.close,
        vwap=current_vwap, adx=current_adx, volume=current_volume,
        required_volume=required_volume, gates=gates,
        summary=("Structural setup is ready; checking option liquidity and premium momentum."
                 if ready else "No complete ORB/VWAP structural setup on the latest closed five-minute bar."),
    )


def _today_completed_candles(bars: list[PriceBar], now_ist: datetime) -> list[SpotCandle]:
    candles = [
        SpotCandle(
            timestamp=bar.timestamp.astimezone(IST), open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume,
        )
        for bar in bars
        if bar.timestamp.astimezone(IST).date() == now_ist.date()
        and bar.timestamp.astimezone(IST) + timedelta(minutes=1) <= now_ist
    ]
    candles.sort(key=lambda item: item.timestamp)
    return candles


def _within(moment: datetime, start: str, end: str) -> bool:
    start_hour, start_minute = (int(value) for value in start.split(":"))
    end_hour, end_minute = (int(value) for value in end.split(":"))
    value = moment.hour * 60 + moment.minute
    return start_hour * 60 + start_minute <= value <= end_hour * 60 + end_minute
