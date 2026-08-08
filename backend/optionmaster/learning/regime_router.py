from dataclasses import dataclass
from datetime import datetime, timezone

from optionmaster.execution.mode import ExecutionMode
from optionmaster.journal.store import TradeJournal
from optionmaster.market.models import MarketFeatures, Regime
from optionmaster.market.regime_learning import (
    RegimeObservation,
    RegimePerformanceReport,
    RegimeStrategyPerformance,
    RegimeStrategyRecommendation,
)
from optionmaster.strategy.profiles import BASELINE_PROFILE, StrategyProfile
from optionmaster.strategy.regime import detect_regime


@dataclass(frozen=True, slots=True)
class RegimeStrategySelection:
    regime: Regime
    profile: StrategyProfile
    observation: RegimeObservation


class RegimeStrategyRouter:
    """Routes paper setups only after a strategy proves itself in that regime."""

    minimum_closed_trades = 30
    minimum_losses = 5
    minimum_profit_factor = 1.15
    minimum_win_rate_pct = 45.0

    def __init__(self, journal: TradeJournal, profiles) -> None:
        self._journal = journal
        self._profiles = profiles

    def select(self, *, features: MarketFeatures, execution_mode: ExecutionMode) -> RegimeStrategySelection:
        # Regime labels stay independent of any candidate's tighter entry rules.
        regime = detect_regime(features, BASELINE_PROFILE)
        active = self._profiles.active()
        recommendation = self._recommendation_for(regime)
        profile = active
        if execution_mode is ExecutionMode.REAL and active.paper_trial_only:
            profile = BASELINE_PROFILE
            routing_reason = "Paper-trial profile is unavailable in Real mode; using the baseline profile."
        elif execution_mode is ExecutionMode.PAPER and recommendation.evidence_ready and recommendation.strategy_id:
            profile = self._profiles.get(recommendation.strategy_id)
            routing_reason = f"Paper route: {recommendation.reason}"
        elif execution_mode is ExecutionMode.REAL and recommendation.evidence_ready:
            routing_reason = "Real mode keeps the manually active profile; regime routing is paper-only."
        else:
            routing_reason = recommendation.reason
        observation = RegimeObservation(
            recorded_at=datetime.now(timezone.utc),
            symbol=features.symbol.upper(),
            regime=regime,
            confidence=self._confidence(features, regime),
            reasons=self._reasons(features, regime),
            momentum_pct=features.momentum,
            underlying_change_pct=features.underlying_change_pct,
            india_vix=features.india_vix,
            put_call_oi_ratio=features.put_call_oi_ratio,
            liquid_quote_count=features.liquid_quote_count,
            active_strategy_id=active.id,
            routed_strategy_id=profile.id,
            routing_reason=routing_reason,
            execution_mode=execution_mode.value,
        )
        return RegimeStrategySelection(regime=regime, profile=profile, observation=observation)

    def report(self) -> RegimePerformanceReport:
        performances = self._journal.regime_strategy_performance()
        return RegimePerformanceReport(
            latest_observation=self._journal.latest_regime_observation(),
            strategy_performance=performances,
            recommendations=[self._recommendation_for(regime, performances) for regime in Regime],
            minimum_closed_trades=self.minimum_closed_trades,
        )

    def _recommendation_for(
        self, regime: Regime, performances: list[RegimeStrategyPerformance] | None = None
    ) -> RegimeStrategyRecommendation:
        candidates = [
            item for item in (performances if performances is not None else self._journal.regime_strategy_performance())
            if item.regime is regime and self._qualifies(item)
        ]
        if candidates:
            winner = max(candidates, key=lambda item: (item.net_pnl, item.profit_factor or 0, item.win_rate_pct))
            return RegimeStrategyRecommendation(
                regime=regime,
                strategy_id=winner.strategy_id,
                evidence_ready=True,
                closed_trades=winner.closed_trades,
                reason=(
                    f"{winner.strategy_id} has {winner.closed_trades} closed paper trades, "
                    f"{winner.net_pnl:.0f} net after costs, PF {winner.profit_factor:.2f}."
                ),
            )
        available = [
            item for item in (performances if performances is not None else self._journal.regime_strategy_performance())
            if item.regime is regime
        ]
        observed = max((item.closed_trades for item in available), default=0)
        return RegimeStrategyRecommendation(
            regime=regime,
            evidence_ready=False,
            closed_trades=observed,
            reason=(
                f"No regime-specific strategy is proven yet. Need {self.minimum_closed_trades} closed "
                "paper trades, at least 5 losses observed, positive net P&L, PF >= 1.15, and win rate >= 45%."
            ),
        )

    def _qualifies(self, item: RegimeStrategyPerformance) -> bool:
        return (
            item.closed_trades >= self.minimum_closed_trades
            and item.losses >= self.minimum_losses
            and item.net_pnl > 0
            and item.profit_factor is not None
            and item.profit_factor >= self.minimum_profit_factor
            and item.win_rate_pct >= self.minimum_win_rate_pct
        )

    @staticmethod
    def _confidence(features: MarketFeatures, regime: Regime) -> float:
        if regime in (Regime.BULLISH_TREND, Regime.BEARISH_TREND):
            trend = min(abs(features.momentum) / 0.15, 1.0)
            session = min(abs(features.underlying_change_pct) / 0.20, 1.0)
            oi_bias = min(abs(features.put_oi_change - features.call_oi_change) / 100_000, 1.0)
            return round(45 + 25 * trend + 20 * session + 10 * oi_bias, 1)
        if regime is Regime.RANGE_BOUND:
            quiet = max(0.0, 1 - abs(features.momentum) / 0.04)
            return round(55 + 35 * quiet, 1)
        if regime is Regime.HIGH_VOLATILITY:
            return round(min(95.0, 55 + max(features.india_vix - 20, 0) * 4), 1)
        return 35.0

    @staticmethod
    def _reasons(features: MarketFeatures, regime: Regime) -> list[str]:
        if regime is Regime.BULLISH_TREND:
            return ["positive spot momentum", "positive session change", "put OI change leads call OI change"]
        if regime is Regime.BEARISH_TREND:
            return ["negative spot momentum", "negative session change", "call OI change leads put OI change"]
        if regime is Regime.RANGE_BOUND:
            return ["spot momentum and session change are both muted"]
        if regime is Regime.HIGH_VOLATILITY:
            return [f"India VIX is {features.india_vix:.2f} or option liquidity is insufficient"]
        return ["trend and OI confirmation are incomplete"]
