from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from optionmaster.market.models import OptionSide


class TickKind(StrEnum):
    SPOT = "SPOT"
    OPTION = "OPTION"


class ScalpingAction(StrEnum):
    SKIP = "SKIP"
    LIMIT_AT_ASK = "LIMIT_AT_ASK"
    MARKET_ELIGIBLE = "MARKET_ELIGIBLE"


class MarketTick(BaseModel):
    security_id: int = Field(gt=0)
    kind: TickKind
    timestamp: datetime
    ltp: float = Field(gt=0)
    bid: float = Field(default=0, ge=0)
    ask: float = Field(default=0, ge=0)
    last_quantity: int = Field(default=0, ge=0)
    cumulative_volume: int = Field(default=0, ge=0)
    bid_quantity: int = Field(default=0, ge=0)
    ask_quantity: int = Field(default=0, ge=0)


class ScalpingSessionRequest(BaseModel):
    symbol: str
    spot_security_id: int = Field(gt=0)
    option_security_id: int = Field(gt=0)
    option_side: OptionSide
    expiry: date
    spot_segment: str = "IDX_I"
    option_segment: str = "NSE_FNO"
    lot_size: int | None = Field(default=None, gt=0)
    max_market_spread_pct: float = Field(default=0.20, gt=0, le=1)
    max_limit_spread_pct: float = Field(default=0.30, gt=0, le=1)
    min_volume_surge_ratio: float = Field(default=3.0, ge=2.0, le=10.0)


class CvdMetrics(BaseModel):
    cumulative_delta: int = 0
    tick_delta: int = 0
    volume_surge_ratio: float = 0
    inferred_aggressor: str = "UNKNOWN"


class DeltaSyncMetrics(BaseModel):
    available: bool = False
    spot_momentum_pct: float = 0
    option_momentum_pct: float = 0
    premium_leads_spot: bool = False


class ScalpingDecision(BaseModel):
    action: ScalpingAction = ScalpingAction.SKIP
    timestamp: datetime
    option_security_id: int
    option_side: OptionSide
    entry_price: float | None = None
    spread_pct: float | None = None
    cvd: CvdMetrics
    delta_sync: DeltaSyncMetrics
    gamma_lot_multiplier: float
    reason: str
    paper_only: bool = True


class ScalpingSession(BaseModel):
    id: str
    configuration: ScalpingSessionRequest
    running: bool = False
    latest_decision: ScalpingDecision | None = None
    last_error: str | None = None
