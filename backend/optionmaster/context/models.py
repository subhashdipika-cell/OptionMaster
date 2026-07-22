from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from optionmaster.market.models import MarketSnapshot, OptionSide, Signal


class LevelRole(StrEnum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


class LevelKind(StrEnum):
    PREVIOUS_DAY_HIGH = "PREVIOUS_DAY_HIGH"
    PREVIOUS_DAY_LOW = "PREVIOUS_DAY_LOW"
    PIVOT = "PIVOT"
    PIVOT_RESISTANCE = "PIVOT_RESISTANCE"
    PIVOT_SUPPORT = "PIVOT_SUPPORT"
    ROUND_NUMBER = "ROUND_NUMBER"
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    MAX_CALL_OI = "MAX_CALL_OI"
    MAX_PUT_OI = "MAX_PUT_OI"


class ShadowAction(StrEnum):
    NO_DIRECTIONAL_SIGNAL = "NO_DIRECTIONAL_SIGNAL"
    WOULD_ALLOW = "WOULD_ALLOW"
    WOULD_SKIP = "WOULD_SKIP"


class PositionSizeHint(StrEnum):
    NO_ENTRY = "NO_ENTRY"
    STANDARD_PAPER_ONLY = "STANDARD_PAPER_ONLY"
    STRONG_SETUP_REVIEW = "STRONG_SETUP_REVIEW"


class PriceBar(BaseModel):
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(default=0, ge=0)


class StructuralLevel(BaseModel):
    price: float = Field(gt=0)
    role: LevelRole
    kind: LevelKind
    strength: int = Field(default=1, ge=1, le=5)
    note: str


class FreshnessMetrics(BaseModel):
    atr: float = 0
    trigger_price: float = 0
    traveled_points: float = 0
    traveled_atr: float = 0
    signal_age_candles: int = 0
    fresh: bool = False
    entry_preference: str = "WAIT_FOR_CONTEXT"


class ConfluenceBreakdown(BaseModel):
    total: int = Field(ge=0, le=100)
    location: int = Field(ge=0, le=30)
    freshness: int = Field(ge=0, le=25)
    higher_timeframe_trend: int = Field(ge=0, le=20)
    volume_oi: int = Field(ge=0, le=25)


class ContextFilterSettings(BaseModel):
    atr_period: int = Field(default=14, ge=2, le=50)
    max_trigger_extension_atr: float = Field(default=1.25, gt=0, le=3)
    max_signal_age_candles: int = Field(default=3, ge=1, le=20)
    structure_buffer_atr: float = Field(default=0.15, gt=0, le=1)
    target_buffer_atr: float = Field(default=0.10, gt=0, le=1)
    assumed_stop_atr: float = Field(default=0.60, gt=0, le=3)
    minimum_structure_rr: float = Field(default=1.50, gt=0, le=5)
    minimum_confluence_score: int = Field(default=70, ge=1, le=100)
    strong_confluence_score: int = Field(default=85, ge=1, le=100)
    round_number_interval: float = Field(default=500, gt=0)


class ContextEvaluationRequest(BaseModel):
    snapshot: MarketSnapshot
    signal: Signal
    bars: list[PriceBar] = Field(min_length=2)
    signal_origin_price: float | None = Field(default=None, gt=0)
    signal_age_candles: int | None = Field(default=None, ge=0)
    settings: ContextFilterSettings = Field(default_factory=ContextFilterSettings)


class ContextDecision(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime
    symbol: str
    strategy_id: str
    side: OptionSide | None = None
    underlying_price: float
    action: ShadowAction
    position_size_hint: PositionSizeHint
    freshness: FreshnessMetrics
    confluence: ConfluenceBreakdown
    nearest_opposing_level: StructuralLevel | None = None
    nearest_defensive_level: StructuralLevel | None = None
    recommended_underlying_target: float | None = None
    assumed_stop_distance: float | None = None
    risk_reward_to_structure: float | None = None
    levels: list[StructuralLevel] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    shadow_mode: bool = True


class ContextShadowSummary(BaseModel):
    total_evaluations: int
    directional_signals: int
    would_allow: int
    would_skip: int
    average_confluence_score: float
    latest_evaluated_at: datetime | None = None


class FeatureOutcomeBucket(BaseModel):
    label: str
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    net_pnl: float


class ContextOutcomeReport(BaseModel):
    linked_closed_trades: int
    recommended_minimum_sample: int = 200
    ready_for_feature_review: bool
    freshness: list[FeatureOutcomeBucket]
    confluence: list[FeatureOutcomeBucket]
    structure_room: list[FeatureOutcomeBucket]
