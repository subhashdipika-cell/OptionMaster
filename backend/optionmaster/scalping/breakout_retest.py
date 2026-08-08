"""Paper-only 5-minute breakout-retest monitor driven by Dhan live ticks.

The spot feed builds five-minute bars. After a breakout, retest, and confirming
close, the chosen option is evaluated at most once per configured interval.
Entries and exits are recorded only as local paper decisions; no broker endpoint
is called from this component.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from optionmaster.scalping.engine import GammaTuner
from optionmaster.scalping.models import (
    CvdMetrics, DeltaSyncMetrics, MarketTick, ScalpingAction, ScalpingDecision,
    ScalpingSessionRequest, ScalpingStrategy, TickKind,
)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(slots=True)
class _Bar:
    started_at: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class _Pending:
    side: str
    level: float
    index: int


@dataclass(slots=True)
class _Position:
    entry: float
    stop: float
    target: float


class BreakoutRetestScalpingEngine:
    """One option side per session; CE and PE need their own paper sessions."""

    def __init__(self, configuration: ScalpingSessionRequest) -> None:
        self.configuration = configuration
        self._bars: list[_Bar] = []
        self._bar: _Bar | None = None
        self._pending: _Pending | None = None
        self._entry_side: str | None = None
        self._entry_at: datetime | None = None
        self._position: _Position | None = None
        self._last_evaluation: datetime | None = None
        self._previous_option_ltp: float | None = None
        self.latest_decision: ScalpingDecision | None = None

    def ingest(self, tick: MarketTick) -> ScalpingDecision:
        if tick.security_id == self.configuration.spot_security_id:
            if tick.kind is not TickKind.SPOT:
                raise ValueError("Spot security received an option tick.")
            self._spot(tick)
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Waiting for the next 15-second option evaluation.")
        if tick.security_id != self.configuration.option_security_id or tick.kind is not TickKind.OPTION:
            raise ValueError("Tick does not belong to this scalping session.")
        return self._option(tick)

    def _spot(self, tick: MarketTick) -> None:
        bucket = self._bucket(tick.timestamp)
        if self._bar is None:
            self._bar = _Bar(bucket, tick.ltp, tick.ltp, tick.ltp, tick.ltp)
        elif bucket == self._bar.started_at:
            self._bar.high = max(self._bar.high, tick.ltp)
            self._bar.low = min(self._bar.low, tick.ltp)
            self._bar.close = tick.ltp
        elif bucket > self._bar.started_at:
            self._bars.append(self._bar)
            self._on_completed_bar(self._bar)
            self._bar = _Bar(bucket, tick.ltp, tick.ltp, tick.ltp, tick.ltp)

    @staticmethod
    def _bucket(timestamp: datetime) -> datetime:
        local = timestamp.astimezone(IST)
        total = local.hour * 60 + local.minute
        start = 9 * 60 + 15
        aligned = start + max(0, ((total - start) // 5) * 5)
        return local.replace(hour=aligned // 60, minute=aligned % 60, second=0, microsecond=0)

    def _on_completed_bar(self, bar: _Bar) -> None:
        index = len(self._bars) - 1
        if self.configuration.strategy is ScalpingStrategy.BREAKOUT_RETEST_3_BAR:
            self._on_three_bar_pattern(index)
            return
        if self._pending is not None:
            if index - self._pending.index > self.configuration.retest_bars:
                self._pending = None
            elif index > self._pending.index and self._retest(bar, self._pending):
                self._entry_side = self._pending.side
                self._entry_at = bar.started_at + timedelta(minutes=5)
                self._pending = None
                return
        if index < self.configuration.breakout_range_bars:
            return
        prior = self._bars[index - self.configuration.breakout_range_bars:index]
        high, low = max(item.high for item in prior), min(item.low for item in prior)
        threshold = self.configuration.breakout_minimum_pct / 100
        if bar.close >= high * (1 + threshold):
            self._pending = _Pending("CE", high, index)
        elif bar.close <= low * (1 - threshold):
            self._pending = _Pending("PE", low, index)

    def _on_three_bar_pattern(self, index: int) -> None:
        """Reference bar -> breakout bar -> retest/turn bar, all completed."""
        if index < 2:
            return
        reference, breakout, retest = self._bars[index - 2:index + 1]
        threshold = self.configuration.breakout_minimum_pct / 100
        if (
            breakout.close > reference.high * (1 + threshold)
            and retest.low <= reference.high * (1 + self.configuration.retest_tolerance_pct / 100)
            and retest.low >= reference.high * (1 - self.configuration.retest_invalidation_pct / 100)
            and retest.close > reference.high and retest.close > retest.open
        ):
            self._entry_side, self._entry_at = "CE", retest.started_at + timedelta(minutes=5)
        elif (
            breakout.close < reference.low * (1 - threshold)
            and retest.high >= reference.low * (1 - self.configuration.retest_tolerance_pct / 100)
            and retest.high <= reference.low * (1 + self.configuration.retest_invalidation_pct / 100)
            and retest.close < reference.low and retest.close < retest.open
        ):
            self._entry_side, self._entry_at = "PE", retest.started_at + timedelta(minutes=5)

    def _retest(self, bar: _Bar, pending: _Pending) -> bool:
        tolerance = self.configuration.retest_tolerance_pct / 100
        invalidation = self.configuration.retest_invalidation_pct / 100
        if pending.side == "CE":
            return bar.low <= pending.level * (1 + tolerance) and bar.low >= pending.level * (1 - invalidation) and bar.close >= pending.level and bar.close > bar.open
        return bar.high >= pending.level * (1 - tolerance) and bar.high <= pending.level * (1 + invalidation) and bar.close <= pending.level and bar.close < bar.open

    def _option(self, tick: MarketTick) -> ScalpingDecision:
        interval = timedelta(seconds=self.configuration.evaluation_interval_seconds)
        if self._last_evaluation is not None and tick.timestamp - self._last_evaluation < interval:
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Next option check is scheduled at the 15-second interval.")
        previous_ltp = self._previous_option_ltp
        self._previous_option_ltp = tick.ltp
        self._last_evaluation = tick.timestamp
        if self._position is not None:
            if tick.bid > 0 and tick.bid <= self._position.stop:
                closed = self._decision(tick.timestamp, ScalpingAction.PAPER_STOP_EXIT, "Paper stop-loss reached on displayed option bid.")
                self._position = None
                return closed
            if tick.bid > 0 and tick.bid >= self._position.target:
                closed = self._decision(tick.timestamp, ScalpingAction.PAPER_TARGET_EXIT, "Paper target reached on displayed option bid.")
                self._position = None
                return closed
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Paper position remains open; next bid check in 15 seconds.")
        if self._entry_side is None:
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "No completed five-minute breakout-retest setup.")
        if self._entry_side != self.configuration.option_side.value:
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Breakout direction does not match this option-side session.")
        if self._entry_at is None or tick.timestamp > self._entry_at + timedelta(minutes=2):
            self._entry_side, self._entry_at = None, None
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Breakout-retest entry signal expired before a safe option evaluation.")
        if (
            previous_ltp is None or previous_ltp <= 0
            or ((tick.ltp / previous_ltp) - 1) * 100 < self.configuration.option_momentum_minimum_pct
        ):
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Option premium momentum has not confirmed the retest entry.")
        spread = self._spread(tick)
        if spread is None or spread > self.configuration.max_limit_spread_pct or tick.ask <= 0:
            return self._decision(tick.timestamp, ScalpingAction.SKIP, "Option spread is not safe for a paper entry.")
        self._position = _Position(
            tick.ask,
            round(tick.ask * (1 - self.configuration.paper_stop_loss_fraction), 2),
            round(tick.ask * (1 + self.configuration.paper_target_fraction), 2),
        )
        self._entry_side, self._entry_at = None, None
        return self._decision(tick.timestamp, ScalpingAction.PAPER_ENTRY, "Paper breakout-retest entry recorded at displayed option ask.")

    def _decision(self, timestamp: datetime, action: ScalpingAction, reason: str) -> ScalpingDecision:
        position = self._position
        decision = ScalpingDecision(
            action=action, timestamp=timestamp, option_security_id=self.configuration.option_security_id,
            option_side=self.configuration.option_side, entry_price=position.entry if position else None,
            cvd=CvdMetrics(), delta_sync=DeltaSyncMetrics(),
            gamma_lot_multiplier=GammaTuner.lot_multiplier(self.configuration.expiry, timestamp), reason=reason,
            paper_position_open=position is not None,
            paper_stop_loss=position.stop if position else None, paper_target=position.target if position else None,
        )
        self.latest_decision = decision
        return decision

    @staticmethod
    def _spread(tick: MarketTick) -> float | None:
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            return None
        midpoint = (tick.bid + tick.ask) / 2
        return ((tick.ask - tick.bid) / midpoint) * 100 if midpoint else None
