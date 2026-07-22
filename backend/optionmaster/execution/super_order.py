from uuid import uuid4

from pydantic import BaseModel, Field

from optionmaster.costs.calculator import NseOptionCostCalculator, RoundTripCosts
from optionmaster.scalping.models import ScalpingAction, ScalpingDecision


class SuperOrderPlanRequest(BaseModel):
    contract_security_id: int = Field(gt=0)
    exchange_segment: str = "NSE_FNO"
    lots: int = Field(default=1, gt=0)
    quantity: int | None = Field(default=None, gt=0)
    stop_loss_fraction: float = Field(default=0.20, gt=0, le=0.50)
    target_fraction: float = Field(default=0.30, gt=0, le=1.0)
    trailing_fraction: float = Field(default=0.10, gt=0, le=0.50)


class SuperOrderIntent(BaseModel):
    correlation_id: str
    transaction_type: str = "BUY"
    exchange_segment: str
    product_type: str = "INTRADAY"
    order_type: str
    security_id: int
    quantity: int
    price: float
    target_price: float
    stop_loss_price: float
    trailing_jump: float
    estimated_costs_at_target: RoundTripCosts
    estimated_gross_target_pnl: float
    estimated_net_target_pnl: float
    breakeven_points: float
    paper_only: bool = True


def build_super_order_intent(
    decision: ScalpingDecision,
    request: SuperOrderPlanRequest,
    *,
    calculator: NseOptionCostCalculator | None = None,
) -> SuperOrderIntent:
    """Build a Dhan-compatible Super Order payload without sending it."""
    if decision.action not in (ScalpingAction.MARKET_ELIGIBLE, ScalpingAction.LIMIT_AT_ASK):
        raise ValueError("A Super Order intent requires a trade-eligible scalping decision.")
    if not decision.entry_price or decision.entry_price <= 0:
        raise ValueError("A valid entry price is required for Super Order planning.")
    entry = decision.entry_price
    order_type = "MARKET" if decision.action is ScalpingAction.MARKET_ELIGIBLE else "LIMIT"
    target = round(entry * (1 + request.target_fraction), 2)
    result = (calculator or NseOptionCostCalculator()).net_result(
        entry_price=entry,
        exit_price=target,
        quantity=request.quantity,
        underlying=request.exchange_segment,
    )
    return SuperOrderIntent(
        correlation_id=f"OM-{uuid4().hex[:20]}",
        exchange_segment=request.exchange_segment,
        order_type=order_type,
        security_id=request.contract_security_id,
        quantity=request.quantity,
        price=0.0 if order_type == "MARKET" else round(entry, 2),
        target_price=target,
        stop_loss_price=round(entry * (1 - request.stop_loss_fraction), 2),
        trailing_jump=round(max(0.05, entry * request.trailing_fraction), 2),
        estimated_costs_at_target=result.costs,
        estimated_gross_target_pnl=result.gross_pnl,
        estimated_net_target_pnl=result.net_pnl,
        breakeven_points=result.breakeven_points,
    )
