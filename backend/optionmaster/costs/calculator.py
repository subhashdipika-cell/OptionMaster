from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from pydantic import BaseModel


class ChargeBreakdown(BaseModel):
    turnover: float
    brokerage: float
    exchange_transaction_charge: float
    ipft: float
    sebi_turnover_fee: float
    gst: float
    stt: float
    stamp_duty: float
    clearing_charge: float
    total: float


class RoundTripCosts(BaseModel):
    entry: ChargeBreakdown
    exit: ChargeBreakdown
    total: float


class NetTradeResult(BaseModel):
    gross_pnl: float
    costs: RoundTripCosts
    net_pnl: float
    breakeven_points: float


#: Indices that settle on BSE rather than NSE — their exchange transaction
#: charge is lower, so SENSEX/BANKEX must not be billed at the NSE rate.
BSE_UNDERLYINGS = frozenset({"SENSEX", "BANKEX"})


def exchange_for(underlying: str | None) -> str:
    """Resolve the billing exchange from an underlying name OR a Dhan segment.

    Accepts both because callers have different things to hand: the backtest and
    paper broker know the underlying ("SENSEX"), while order planning knows the
    segment ("BSE_FNO").
    """
    value = (underlying or "").upper()
    if not value:
        return "NSE"
    if value.startswith("BSE") or value.startswith("NSE"):
        return "BSE" if value.startswith("BSE") else "NSE"
    return "BSE" if value in BSE_UNDERLYINGS else "NSE"


@dataclass(frozen=True, slots=True)
class NseOptionCostSchedule:
    """Retail index-options cost schedule, parameterised for rate changes.

    Rates are shared with AlphaEdge (`src/engines/costs.js`) and TradingBrain
    (`domains/execution/costs.py`); keep the three in step or the apps' P&L
    stops being comparable.
    """

    brokerage_per_executed_order: float = 20.0
    # Flat per executed order for OPTIONS — the "Rs 20 or 0.03%, whichever is
    # lower" rule is an equity/futures rule and does not apply here.
    exchange_transaction_rate: float = 0.0003503          # NSE, ~Rs 35.03/lakh
    bse_exchange_transaction_rate: float = 0.000325       # BSE, ~Rs 32.5/lakh
    ipft_rate: float = 0.0000050
    sebi_turnover_rate: float = 0.0000010
    gst_rate: float = 0.18
    stt_sell_rate: float = 0.0010   # 0.10% on the sell-side premium since 2024-10-01
    stamp_duty_buy_rate: float = 0.00003
    clearing_charge_per_order: float = 0.0

    def exchange_rate_for(self, underlying: str | None) -> float:
        return (
            self.bse_exchange_transaction_rate
            if exchange_for(underlying) == "BSE"
            else self.exchange_transaction_rate
        )

    @classmethod
    def from_settings(cls, settings) -> "NseOptionCostSchedule":
        return cls(
            brokerage_per_executed_order=settings.nse_option_brokerage_per_order,
            exchange_transaction_rate=settings.nse_option_exchange_transaction_rate,
            ipft_rate=settings.nse_option_ipft_rate,
            sebi_turnover_rate=settings.nse_option_sebi_turnover_rate,
            gst_rate=settings.nse_option_gst_rate,
            stt_sell_rate=settings.nse_option_stt_sell_rate,
            stamp_duty_buy_rate=settings.nse_option_stamp_duty_buy_rate,
            clearing_charge_per_order=settings.nse_option_clearing_charge_per_order,
            bse_exchange_transaction_rate=settings.bse_option_exchange_transaction_rate,
        )


class NseOptionCostCalculator:
    def __init__(self, schedule: NseOptionCostSchedule | None = None) -> None:
        self.schedule = schedule or NseOptionCostSchedule()

    def leg_costs(
        self, *, price: float, quantity: int, is_sell: bool, underlying: str | None = None
    ) -> ChargeBreakdown:
        if price <= 0 or quantity <= 0:
            raise ValueError("Price and quantity must be positive when estimating trade costs.")
        turnover = price * quantity
        brokerage = self.schedule.brokerage_per_executed_order
        exchange = self._paise(turnover * self.schedule.exchange_rate_for(underlying))
        ipft = self._paise(turnover * self.schedule.ipft_rate)
        sebi = self._paise(turnover * self.schedule.sebi_turnover_rate)
        clearing = self._paise(self.schedule.clearing_charge_per_order)
        gst = self._paise((brokerage + exchange + ipft + sebi + clearing) * self.schedule.gst_rate)
        stt = self._rupee(turnover * self.schedule.stt_sell_rate) if is_sell else 0.0
        stamp = self._rupee(turnover * self.schedule.stamp_duty_buy_rate) if not is_sell else 0.0
        total = self._paise(brokerage + exchange + ipft + sebi + clearing + gst + stt + stamp)
        return ChargeBreakdown(
            turnover=self._paise(turnover),
            brokerage=self._paise(brokerage),
            exchange_transaction_charge=exchange,
            ipft=ipft,
            sebi_turnover_fee=sebi,
            gst=gst,
            stt=stt,
            stamp_duty=stamp,
            clearing_charge=clearing,
            total=total,
        )

    def round_trip(
        self, *, entry_price: float, exit_price: float, quantity: int,
        underlying: str | None = None,
    ) -> RoundTripCosts:
        entry = self.leg_costs(
            price=entry_price, quantity=quantity, is_sell=False, underlying=underlying
        )
        exit = self.leg_costs(
            price=exit_price, quantity=quantity, is_sell=True, underlying=underlying
        )
        return RoundTripCosts(entry=entry, exit=exit, total=self._paise(entry.total + exit.total))

    def net_result(
        self, *, entry_price: float, exit_price: float, quantity: int,
        underlying: str | None = None,
    ) -> NetTradeResult:
        gross = self._paise((exit_price - entry_price) * quantity)
        costs = self.round_trip(
            entry_price=entry_price, exit_price=exit_price, quantity=quantity,
            underlying=underlying,
        )
        net = self._paise(gross - costs.total)
        return NetTradeResult(
            gross_pnl=gross,
            costs=costs,
            net_pnl=net,
            breakeven_points=round(costs.total / quantity, 4),
        )

    @staticmethod
    def _paise(value: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _rupee(value: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
