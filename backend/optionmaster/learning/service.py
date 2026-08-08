from pydantic import BaseModel, Field

from optionmaster.journal.store import PerformanceSummary, TradeJournal
from optionmaster.strategy.profiles import BASELINE_PROFILE, BUILT_IN_PROFILES, StrategyProfile


class PromotionPolicy(BaseModel):
    """Minimum forward-paper evidence needed before changing the active paper profile."""

    minimum_closed_trades: int = Field(default=30, ge=1)
    minimum_losses_observed: int = Field(default=5, ge=1)
    minimum_profit_factor: float = Field(default=1.15, gt=0)
    minimum_win_rate_pct: float = Field(default=45, ge=0, le=100)


class ProfileEvaluation(BaseModel):
    profile: StrategyProfile
    performance: PerformanceSummary
    eligible_for_paper_promotion: bool
    reasons: list[str]
    evidence_scope: str = "closed forward paper trades only"


class AutoPromotionResult(BaseModel):
    promoted: bool
    active_profile: StrategyProfile
    evaluations: list[ProfileEvaluation]
    reason: str


class UnknownStrategyProfile(ValueError):
    """Raised when a caller references a profile OptionMaster does not own."""


class PaperPromotionRejected(ValueError):
    """Raised when forward-paper evidence is insufficient for a profile change."""


class PaperTrialRejected(ValueError):
    """Raised when a profile is not explicitly designated for paper trial use."""


class StrategyProfileRegistry:
    """Small, explicit profile catalogue; the active ID is persisted in the trade journal."""

    def __init__(self, journal: TradeJournal) -> None:
        self._journal = journal
        self._profiles = {profile.id: profile for profile in BUILT_IN_PROFILES}
        self._journal.get_active_strategy_id(default=BASELINE_PROFILE.id)

    def list(self) -> list[StrategyProfile]:
        return list(self._profiles.values())

    def get(self, profile_id: str) -> StrategyProfile:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise UnknownStrategyProfile(f"Unknown strategy profile: {profile_id}.")
        return profile

    def active(self) -> StrategyProfile:
        profile_id = self._journal.get_active_strategy_id(default=BASELINE_PROFILE.id)
        if profile_id not in self._profiles:
            self._journal.set_active_strategy_id(BASELINE_PROFILE.id)
            return BASELINE_PROFILE
        return self._profiles[profile_id]

    def activate(self, profile_id: str) -> StrategyProfile:
        profile = self.get(profile_id)
        self._journal.set_active_strategy_id(profile.id)
        return profile

    def start_paper_trial(self, profile_id: str) -> StrategyProfile:
        """Activate a deliberately paper-only profile without calling it a promotion."""
        profile = self.get(profile_id)
        if not profile.paper_trial_only:
            raise PaperTrialRejected("Only designated paper-trial profiles can be started this way.")
        return self.activate(profile.id)


class LearningService:
    """Evaluates profile results without changing live execution behavior."""

    def __init__(
        self,
        *,
        journal: TradeJournal,
        profiles: StrategyProfileRegistry,
        policy: PromotionPolicy | None = None,
    ) -> None:
        self._journal = journal
        self._profiles = profiles
        self._policy = policy or PromotionPolicy()

    def evaluate(self, profile_id: str) -> ProfileEvaluation:
        profile = self._profiles.get(profile_id)
        performance = self._journal.performance("paper", strategy_id=profile.id)
        reasons: list[str] = []
        if performance.closed_trades < self._policy.minimum_closed_trades:
            reasons.append(
                f"Need at least {self._policy.minimum_closed_trades} closed forward paper trades; "
                f"only {performance.closed_trades} are recorded."
            )
        if performance.losses < self._policy.minimum_losses_observed:
            reasons.append(
                f"Need at least {self._policy.minimum_losses_observed} observed losses to assess downside; "
                f"only {performance.losses} are recorded."
            )
        if performance.net_pnl <= 0:
            reasons.append("Net P&L after costs must be positive.")
        if performance.profit_factor is None or performance.profit_factor < self._policy.minimum_profit_factor:
            reasons.append(
                f"Profit factor must be at least {self._policy.minimum_profit_factor:.2f} after costs."
            )
        if performance.win_rate_pct < self._policy.minimum_win_rate_pct:
            reasons.append(
                f"Win rate must be at least {self._policy.minimum_win_rate_pct:.0f}% after costs."
            )
        return ProfileEvaluation(
            profile=profile,
            performance=performance,
            eligible_for_paper_promotion=not reasons,
            reasons=reasons,
        )

    def activate_if_eligible(self, profile_id: str) -> StrategyProfile:
        profile = self._profiles.get(profile_id)
        if profile.baseline:
            return self._profiles.activate(profile.id)
        evaluation = self.evaluate(profile.id)
        if not evaluation.eligible_for_paper_promotion:
            raise PaperPromotionRejected(" ".join(evaluation.reasons))
        return self._profiles.activate(profile.id)

    def review_and_promote(self) -> AutoPromotionResult:
        evaluations = [self.evaluate(profile.id) for profile in self._profiles.list() if not profile.baseline]
        eligible = [item for item in evaluations if item.eligible_for_paper_promotion]
        if not eligible:
            return AutoPromotionResult(
                promoted=False,
                active_profile=self._profiles.active(),
                evaluations=evaluations,
                reason="No candidate satisfies the forward-paper promotion policy.",
            )
        winner = max(eligible, key=lambda item: item.performance.net_pnl)
        active = self._profiles.activate(winner.profile.id)
        return AutoPromotionResult(
            promoted=True,
            active_profile=active,
            evaluations=evaluations,
            reason="Activated the eligible candidate with the strongest net paper P&L.",
        )
