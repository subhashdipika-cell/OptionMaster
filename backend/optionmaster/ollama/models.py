from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from optionmaster.market.models import OptionSide


class OllamaVerdict(StrEnum):
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    SKIP = "SKIP"


class OllamaReview(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    recorded_at: datetime
    model: str
    context_decision_id: str | None = None
    symbol: str
    side: OptionSide
    verdict: OllamaVerdict
    confidence: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=600)
    risk_flags: list[str] = Field(default_factory=list, max_length=5)


class OllamaStatus(BaseModel):
    enabled: bool
    available: bool
    model: str
    host: str
    last_error: str | None = None


class OllamaReviewSummary(BaseModel):
    total_reviews: int
    allow_count: int
    review_count: int
    skip_count: int
    linked_closed_trades: int
    linked_net_pnl: float
