from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class OptionSide(StrEnum):
    CE = "CE"
    PE = "PE"


class OptionQuote(BaseModel):
    security_id: int | None = None
    strike: float
    side: OptionSide
    ltp: float = Field(ge=0)
    bid: float = Field(default=0, ge=0)
    ask: float = Field(default=0, ge=0)
    delta: float = 0
    gamma: float = 0
    theta: float = 0
    vega: float = 0
    iv: float = Field(default=0, ge=0)
    oi: float = Field(default=0, ge=0)
    previous_oi: float = Field(default=0, ge=0)
    volume: float = Field(default=0, ge=0)

    @property
    def oi_change(self) -> float:
        return self.oi - self.previous_oi

    @property
    def spread_fraction(self) -> float:
        if self.ltp <= 0 or self.ask <= 0 or self.bid <= 0:
            return 1.0
        return max(0.0, self.ask - self.bid) / self.ltp


class MarketSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    underlying: float = Field(gt=0)
    underlying_change_pct: float = 0
    underlying_momentum: float = 0
    india_vix: float = Field(default=0, ge=0)
    option_quotes: list[OptionQuote] = Field(default_factory=list)


class MarketFeatures(BaseModel):
    symbol: str
    timestamp: datetime
    underlying_change_pct: float
    momentum: float
    india_vix: float
    call_oi_change: float
    put_oi_change: float
    put_call_oi_ratio: float
    average_iv: float
    liquid_quote_count: int


class Regime(StrEnum):
    BULLISH_TREND = "BULLISH_TREND"
    BEARISH_TREND = "BEARISH_TREND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    RANGE_BOUND = "RANGE_BOUND"
    NO_TRADE = "NO_TRADE"


class Signal(BaseModel):
    symbol: str
    timestamp: datetime
    regime: Regime
    strategy_id: str = "baseline-v1"
    side: OptionSide | None = None
    strike: float | None = None
    score: float = 0
    quantity: int = 0
    reason: str
    paper_only: bool = True


class AnalysisResult(BaseModel):
    snapshot: MarketSnapshot
    features: MarketFeatures
    signal: Signal
