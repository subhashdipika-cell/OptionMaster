from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import fmean

from optionmaster.scalping.models import (
    CvdMetrics,
    DeltaSyncMetrics,
    MarketTick,
    ScalpingAction,
    ScalpingDecision,
    ScalpingSessionRequest,
    TickKind,
)


@dataclass(slots=True)
class _CvdSample:
    timestamp: datetime
    signed_quantity: int
    absolute_quantity: int


class CvdTracker:
    """Infers aggressive order flow from trade location and tick direction.

    Dhan's full feed provides LTQ, cumulative volume, best bid and best ask,
    not exchange-confirmed aggressor flags. This is therefore an inference,
    never a claim that a specific participant is institutional.
    """

    def __init__(self, *, window_seconds: float = 10.0, baseline_ticks: int = 20) -> None:
        self._window_seconds = window_seconds
        self._baseline_ticks = baseline_ticks
        self._samples: deque[_CvdSample] = deque(maxlen=400)
        self._previous_volume: int | None = None
        self._previous_ltp: float | None = None

    def update(self, tick: MarketTick) -> CvdMetrics:
        quantity = tick.last_quantity
        if quantity <= 0 and self._previous_volume is not None:
            quantity = max(0, tick.cumulative_volume - self._previous_volume)
        self._previous_volume = tick.cumulative_volume

        aggressor = self._classify(tick)
        signed = quantity if aggressor == "BUY" else -quantity if aggressor == "SELL" else 0
        self._previous_ltp = tick.ltp
        self._samples.append(_CvdSample(tick.timestamp, signed, quantity))
        cutoff = tick.timestamp - timedelta(seconds=self._window_seconds)
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

        history = list(self._samples)
        previous = [sample.absolute_quantity for sample in history[:-1] if sample.absolute_quantity > 0]
        baseline = fmean(previous[-self._baseline_ticks :]) if len(previous) >= 5 else 0.0
        surge = round(quantity / baseline, 3) if baseline > 0 else 0.0
        return CvdMetrics(
            cumulative_delta=sum(sample.signed_quantity for sample in self._samples),
            tick_delta=signed,
            volume_surge_ratio=surge,
            inferred_aggressor=aggressor,
        )

    def _classify(self, tick: MarketTick) -> str:
        tolerance = max(0.01, tick.ltp * 0.0001)
        if tick.ask > 0 and tick.ltp >= tick.ask - tolerance:
            return "BUY"
        if tick.bid > 0 and tick.ltp <= tick.bid + tolerance:
            return "SELL"
        if self._previous_ltp is None:
            return "UNKNOWN"
        if tick.ltp > self._previous_ltp:
            return "BUY"
        if tick.ltp < self._previous_ltp:
            return "SELL"
        return "UNKNOWN"


class DeltaSyncTracker:
    def __init__(self, *, window_seconds: float = 1.0) -> None:
        self._window_seconds = window_seconds
        self._spot: deque[MarketTick] = deque(maxlen=200)
        self._option: deque[MarketTick] = deque(maxlen=200)

    def update(self, tick: MarketTick) -> DeltaSyncMetrics:
        series = self._spot if tick.kind is TickKind.SPOT else self._option
        series.append(tick)
        cutoff = tick.timestamp - timedelta(seconds=self._window_seconds)
        for current in (self._spot, self._option):
            while current and current[0].timestamp < cutoff:
                current.popleft()
        if not self._spot or not self._option:
            return DeltaSyncMetrics()
        elapsed = min(
            (self._spot[-1].timestamp - self._spot[0].timestamp).total_seconds(),
            (self._option[-1].timestamp - self._option[0].timestamp).total_seconds(),
        )
        if elapsed < 0.7:
            return DeltaSyncMetrics()
        spot_momentum = self._momentum(self._spot)
        option_momentum = self._momentum(self._option)
        premium_leads = abs(option_momentum) >= 0.25 and abs(spot_momentum) <= 0.035
        return DeltaSyncMetrics(
            available=True,
            spot_momentum_pct=round(spot_momentum, 4),
            option_momentum_pct=round(option_momentum, 4),
            premium_leads_spot=premium_leads,
        )

    @staticmethod
    def _momentum(series: deque[MarketTick]) -> float:
        start = series[0].ltp
        return ((series[-1].ltp / start) - 1) * 100 if start > 0 else 0.0


class GammaTuner:
    @staticmethod
    def lot_multiplier(expiry: date, now: datetime) -> float:
        days_to_expiry = (expiry - now.date()).days
        if days_to_expiry <= 0:
            return 1.0
        if days_to_expiry == 1:
            return 0.75
        if days_to_expiry <= 3:
            return 0.50
        return 0.25


class ScalpingEngine:
    """Decision-only engine; no broker API is called from this component."""

    def __init__(self, configuration: ScalpingSessionRequest) -> None:
        self.configuration = configuration
        self._cvd = CvdTracker()
        self._delta_sync = DeltaSyncTracker()
        self.latest_decision: ScalpingDecision | None = None

    def ingest(self, tick: MarketTick) -> ScalpingDecision:
        if tick.security_id == self.configuration.spot_security_id:
            if tick.kind is not TickKind.SPOT:
                raise ValueError("Spot security received an option tick.")
            self._delta_sync.update(tick)
            return self._waiting(tick.timestamp, "Waiting for an option tick.")
        if tick.security_id != self.configuration.option_security_id or tick.kind is not TickKind.OPTION:
            raise ValueError("Tick does not belong to this scalping session.")

        delta_sync = self._delta_sync.update(tick)
        cvd = self._cvd.update(tick)
        gamma_multiplier = GammaTuner.lot_multiplier(self.configuration.expiry, tick.timestamp)
        spread_pct = self._spread_pct(tick)
        action, reason = self._decide(tick, cvd, delta_sync, spread_pct)
        decision = ScalpingDecision(
            action=action,
            timestamp=tick.timestamp,
            option_security_id=tick.security_id,
            option_side=self.configuration.option_side,
            entry_price=tick.ask if action is ScalpingAction.LIMIT_AT_ASK else tick.ltp if action is ScalpingAction.MARKET_ELIGIBLE else None,
            spread_pct=spread_pct,
            cvd=cvd,
            delta_sync=delta_sync,
            gamma_lot_multiplier=gamma_multiplier,
            reason=reason,
        )
        self.latest_decision = decision
        return decision

    def _waiting(self, timestamp: datetime, reason: str) -> ScalpingDecision:
        decision = ScalpingDecision(
            timestamp=timestamp,
            option_security_id=self.configuration.option_security_id,
            option_side=self.configuration.option_side,
            cvd=CvdMetrics(),
            delta_sync=DeltaSyncMetrics(),
            gamma_lot_multiplier=GammaTuner.lot_multiplier(self.configuration.expiry, timestamp),
            reason=reason,
        )
        self.latest_decision = decision
        return decision

    def _decide(
        self,
        tick: MarketTick,
        cvd: CvdMetrics,
        delta_sync: DeltaSyncMetrics,
        spread_pct: float | None,
    ) -> tuple[ScalpingAction, str]:
        if not delta_sync.available:
            return ScalpingAction.SKIP, "Waiting for a complete one-second spot/option comparison window."
        if not delta_sync.premium_leads_spot:
            return ScalpingAction.SKIP, "No qualifying option-premium divergence while spot is flat."
        if cvd.inferred_aggressor != "BUY" or cvd.cumulative_delta <= 0:
            return ScalpingAction.SKIP, "Inferred aggressive buying is not confirmed by CVD."
        if cvd.volume_surge_ratio < self.configuration.min_volume_surge_ratio:
            return ScalpingAction.SKIP, "Option volume surge is below the configured threshold."
        if spread_pct is None:
            return ScalpingAction.SKIP, "No valid bid/ask quote for execution safety."
        if spread_pct > self.configuration.max_limit_spread_pct:
            return ScalpingAction.SKIP, "Spread is too wide; wait for liquidity to improve."
        if spread_pct > self.configuration.max_market_spread_pct:
            return ScalpingAction.LIMIT_AT_ASK, "Signal confirmed; market order blocked by spread, use limit at ask."
        return ScalpingAction.MARKET_ELIGIBLE, "Signal confirmed with acceptable spread for a paper market fill."

    @staticmethod
    def _spread_pct(tick: MarketTick) -> float | None:
        if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
            return None
        midpoint = (tick.bid + tick.ask) / 2
        return round(((tick.ask - tick.bid) / midpoint) * 100, 4) if midpoint else None
