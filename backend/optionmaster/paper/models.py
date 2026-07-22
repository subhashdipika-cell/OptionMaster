from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from optionmaster.costs.calculator import RoundTripCosts
from optionmaster.market.models import AnalysisResult, OptionSide


class PaperTradeStatus(StrEnum):
    OPEN = "OPEN"
    TARGET_HIT = "TARGET_HIT"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    CLOSED = "CLOSED"


class CreatePaperTradeRequest(BaseModel):
    symbol: str
    security_id: int = Field(gt=0)
    segment: str = Field(min_length=1)
    instrument_type: str = Field(default="INDEX", min_length=1)
    expiry: str = Field(min_length=10, max_length=10)
    lots: int = Field(default=1, gt=0, description="Number of current exchange lots to simulate.")
    quantity: int | None = Field(
        default=None,
        gt=0,
        description="Optional exchange quantity. Must be a multiple of the current lot size.",
    )
    capital: float = Field(gt=0, description="Paper-trading capital in rupees.")
    stop_loss_fraction: float = Field(default=0.20, gt=0, le=0.50)
    target_fraction: float = Field(default=0.30, gt=0, le=1.00)
    max_risk_fraction: float = Field(default=0.01, gt=0, le=0.02)
    max_premium_fraction: float = Field(default=0.20, gt=0, le=0.50)


class PaperTrade(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    symbol: str
    strategy_id: str = "baseline-v1"
    context_decision_id: str | None = None
    underlying_security_id: int
    underlying_segment: str
    underlying_instrument_type: str
    contract_security_id: int
    lot_size: int
    expiry: str
    side: OptionSide
    strike: float
    quantity: int
    status: PaperTradeStatus = PaperTradeStatus.OPEN
    opened_at: datetime
    entry_price: float
    current_price: float
    stop_loss: float
    target: float
    premium_paid: float
    maximum_risk: float
    gross_pnl: float = 0
    unrealized_pnl: float = 0
    realized_pnl: float | None = None
    charges: RoundTripCosts | None = None
    closed_at: datetime | None = None
    exit_price: float | None = None
    rationale: str


class PaperTradeDecision(BaseModel):
    accepted: bool
    reason: str
    analysis: AnalysisResult
    trade: PaperTrade | None = None
