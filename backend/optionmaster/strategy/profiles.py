from pydantic import BaseModel, Field


class StrategyProfile(BaseModel):
    """A versioned, deterministic rule set used to label every evaluation."""

    id: str = Field(pattern=r"^[a-z0-9-]{3,64}$")
    name: str
    description: str
    baseline: bool = False
    minimum_liquid_quotes: int = Field(default=3, ge=1)
    max_india_vix: float = Field(default=26, gt=0)
    extreme_india_vix: float = Field(default=30, gt=0)
    minimum_momentum_pct: float = Field(default=0.05, gt=0)
    minimum_underlying_change_pct: float = Field(default=0.03, gt=0)
    minimum_option_delta: float = Field(default=0.25, gt=0, le=1)
    maximum_option_delta: float = Field(default=0.65, gt=0, le=1)
    maximum_option_spread_fraction: float = Field(default=0.03, gt=0, le=1)
    minimum_signal_score: float = Field(default=0.65, ge=0, le=1)
    risk_gate_minimum_score: float = Field(default=0.60, ge=0, le=1)


BASELINE_PROFILE = StrategyProfile(
    id="baseline-v1",
    name="Baseline directional v1",
    description="Current OI, momentum, delta, and liquidity rules retained as the reference profile.",
    baseline=True,
)

TIGHT_MOMENTUM_PROFILE = StrategyProfile(
    id="tight-momentum-v1",
    name="Tight momentum candidate v1",
    description="Candidate that requires stronger momentum and materially tighter option liquidity.",
    minimum_liquid_quotes=4,
    max_india_vix=22,
    minimum_momentum_pct=0.07,
    minimum_underlying_change_pct=0.04,
    minimum_option_delta=0.35,
    maximum_option_delta=0.60,
    maximum_option_spread_fraction=0.003,
    minimum_signal_score=0.72,
    risk_gate_minimum_score=0.70,
)


BUILT_IN_PROFILES: tuple[StrategyProfile, ...] = (BASELINE_PROFILE, TIGHT_MOMENTUM_PROFILE)
