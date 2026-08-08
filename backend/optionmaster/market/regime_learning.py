from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from optionmaster.market.models import Regime


class RegimeObservation(BaseModel):
    """A durable, explainable classification of the market at decision time."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    recorded_at: datetime
    symbol: str
    regime: Regime
    confidence: float = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    momentum_pct: float
    underlying_change_pct: float
    india_vix: float
    put_call_oi_ratio: float
    liquid_quote_count: int
    active_strategy_id: str
    routed_strategy_id: str
    routing_reason: str
    execution_mode: str


class RegimeStrategyPerformance(BaseModel):
    regime: Regime
    strategy_id: str
    closed_trades: int
    gross_pnl: float
    charges: float
    net_pnl: float
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float | None = None


class RegimeStrategyRecommendation(BaseModel):
    regime: Regime
    strategy_id: str | None = None
    evidence_ready: bool
    reason: str
    closed_trades: int = 0


class RegimePerformanceReport(BaseModel):
    latest_observation: RegimeObservation | None = None
    strategy_performance: list[RegimeStrategyPerformance] = Field(default_factory=list)
    recommendations: list[RegimeStrategyRecommendation] = Field(default_factory=list)
    minimum_closed_trades: int
    evidence_scope: str = "closed forward paper trades, net of all recorded costs"
