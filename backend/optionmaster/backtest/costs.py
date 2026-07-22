from pydantic import BaseModel, Field

from optionmaster.costs.calculator import NetTradeResult, NseOptionCostCalculator


class BacktestOptionTradeRequest(BaseModel):
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    strategy_id: str = Field(default="baseline-v1", pattern=r"^[a-z0-9-]{3,64}$")
    # Drives the exchange transaction rate: SENSEX/BANKEX bill at the BSE rate.
    # Optional so existing callers keep working; they get the NSE rate.
    symbol: str | None = None


def evaluate_backtest_option_trade(
    request: BacktestOptionTradeRequest, *, calculator: NseOptionCostCalculator
) -> NetTradeResult:
    """Apply the production cost model to every completed historical trade."""
    return calculator.net_result(
        entry_price=request.entry_price,
        exit_price=request.exit_price,
        quantity=request.quantity,
        underlying=request.symbol,
    )
