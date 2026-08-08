from datetime import datetime, timedelta, timezone
from threading import RLock

from optionmaster.costs.calculator import NseOptionCostCalculator
from optionmaster.journal.store import TradeJournal
from optionmaster.market.models import MarketSnapshot, Signal
from optionmaster.paper.models import CreatePaperTradeRequest, PaperTrade, PaperTradeStatus


class PaperTradeRejected(ValueError):
    """Raised when a paper order does not pass the safety gates."""


IST = timezone(timedelta(hours=5, minutes=30))


class PaperBroker:
    """In-memory, option-buying-only paper broker with realistic bid/ask fills."""

    def __init__(
        self,
        *,
        cost_calculator: NseOptionCostCalculator | None = None,
        journal: TradeJournal | None = None,
        notifier=None,
    ) -> None:
        self._trades: dict[str, PaperTrade] = {}
        self._lock = RLock()
        self._costs = cost_calculator or NseOptionCostCalculator()
        self._journal = journal
        self._notifier = notifier

    def list_trades(self) -> list[PaperTrade]:
        with self._lock:
            return sorted(self._trades.values(), key=lambda trade: trade.opened_at, reverse=True)

    def get_trade(self, trade_id: str) -> PaperTrade | None:
        with self._lock:
            return self._trades.get(trade_id)

    def open_from_signal(
        self,
        *,
        request: CreatePaperTradeRequest,
        signal: Signal,
        snapshot: MarketSnapshot,
        quantity: int,
        lot_size: int,
        contract_security_id: int,
        strategy_id: str = "baseline-v1",
        minimum_signal_score: float = 0.65,
        context_decision_id: str | None = None,
    ) -> PaperTrade:
        if not signal.side or signal.strike is None or signal.score < minimum_signal_score:
            raise PaperTradeRejected("Signal is not strong enough for a paper entry.")
        if any(trade.status is PaperTradeStatus.OPEN for trade in self.list_trades()):
            raise PaperTradeRejected("Only one paper position may be open at a time.")

        quote = next(
            (
                item
                for item in snapshot.option_quotes
                if item.side is signal.side and item.strike == signal.strike
            ),
            None,
        )
        entry, stop_loss, target, premium_paid, maximum_risk, initial_costs = self.entry_risk_check(
            request=request, quote=quote, quantity=quantity
        )

        trade = PaperTrade(
            symbol=request.symbol.upper(),
            strategy_id=strategy_id,
            regime=signal.regime,
            context_decision_id=context_decision_id,
            underlying_security_id=request.security_id,
            underlying_segment=request.segment,
            underlying_instrument_type=request.instrument_type,
            expiry=request.expiry,
            side=signal.side,
            strike=signal.strike,
            quantity=quantity,
            contract_security_id=contract_security_id,
            lot_size=lot_size,
            opened_at=datetime.now(timezone.utc),
            entry_price=entry,
            current_price=entry,
            stop_loss=stop_loss,
            target=target,
            premium_paid=premium_paid,
            maximum_risk=maximum_risk,
            unrealized_pnl=-initial_costs.total,
            charges=initial_costs,
            rationale=signal.reason,
        )
        with self._lock:
            self._trades[trade.id] = trade
        if self._journal is not None:
            self._journal.record_paper_trade(trade)
        if self._notifier is not None:
            self._notifier.notify_open(trade)
        return trade

    def entry_risk_check(
        self,
        *,
        request: CreatePaperTradeRequest,
        quote,
        quantity: int,
    ) -> tuple[float, float, float, float, float, object]:
        """Validate one exchange-lot entry without opening a simulated position.

        Paper and real execution share this check so premium and stop-loss
        limits remain in force regardless of the selected execution mode.
        """
        if self._daily_realized_net_pnl() <= -(request.capital * request.daily_loss_fraction):
            raise PaperTradeRejected("Daily loss limit has been reached; new entries are locked for today.")
        if quote is None or quote.ask <= 0 or quote.bid <= 0:
            raise PaperTradeRejected("Selected option has no usable bid/ask quote.")
        entry = quote.ask
        stop_loss = round(entry * (1 - request.stop_loss_fraction), 2)
        target = round(entry * (1 + request.target_fraction), 2)
        premium_paid = round(entry * quantity, 2)
        initial_costs = self._costs.round_trip(
            entry_price=entry, exit_price=entry, quantity=quantity,
            underlying=request.symbol,
        )
        costs_at_stop = self._costs.round_trip(
            entry_price=entry, exit_price=stop_loss, quantity=quantity,
            underlying=request.symbol,
        )
        maximum_risk = round((entry - stop_loss) * quantity + costs_at_stop.total, 2)
        if premium_paid + initial_costs.entry.total > request.capital * request.max_premium_fraction:
            raise PaperTradeRejected("Premium outlay exceeds the configured capital allocation limit.")
        if maximum_risk > request.capital * request.max_risk_fraction:
            raise PaperTradeRejected("Stop-loss risk exceeds the configured per-trade risk limit.")
        return entry, stop_loss, target, premium_paid, maximum_risk, initial_costs

    def _daily_realized_net_pnl(self) -> float:
        """Net closed paper P&L for the current NSE trading date, including restarts."""
        if self._journal is None:
            return 0.0
        today = datetime.now(IST).date()
        total = 0.0
        for trade in self._journal.list_paper_trades():
            if trade.closed_at is None or trade.realized_pnl is None:
                continue
            closed = trade.closed_at
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
            if closed.astimezone(IST).date() == today:
                total += trade.realized_pnl
        return round(total, 2)

    def mark_to_market(self, trade_id: str, snapshot: MarketSnapshot) -> PaperTrade:
        with self._lock:
            trade = self._trades.get(trade_id)
            if trade is None:
                raise KeyError(trade_id)
            if trade.status is not PaperTradeStatus.OPEN:
                return trade

            quote = next(
                (
                    item
                    for item in snapshot.option_quotes
                    if item.side is trade.side and item.strike == trade.strike
                ),
                None,
            )
            if quote is None or quote.bid <= 0:
                raise PaperTradeRejected("No usable bid quote available for paper mark-to-market.")

            exit_value = quote.bid  # buyer exits against the displayed bid
            result = self._costs.net_result(
                entry_price=trade.entry_price, exit_price=exit_value, quantity=trade.quantity,
                underlying=trade.symbol,
            )
            trade.current_price = exit_value
            trade.gross_pnl = result.gross_pnl
            trade.unrealized_pnl = result.net_pnl
            trade.charges = result.costs
            if exit_value <= trade.stop_loss:
                self._close(trade, exit_value, PaperTradeStatus.STOP_LOSS_HIT, result.net_pnl)
            elif exit_value >= trade.target:
                self._close(trade, exit_value, PaperTradeStatus.TARGET_HIT, result.net_pnl)
            if self._journal is not None:
                self._journal.record_paper_trade(trade)
            if self._notifier is not None and trade.status is not PaperTradeStatus.OPEN:
                self._notifier.notify_close(trade)
            return trade

    def square_off(self, trade_id: str, snapshot: MarketSnapshot) -> PaperTrade:
        """Force-close an OPEN trade at the current bid (intraday square-off).

        Same exit mechanics as mark_to_market, but unconditional: used by the
        auto trader at the 15:10 IST cutoff so no paper position rides
        overnight theta. Status CLOSED distinguishes it from stop/target.
        """
        with self._lock:
            trade = self._trades.get(trade_id)
            if trade is None:
                raise KeyError(trade_id)
            if trade.status is not PaperTradeStatus.OPEN:
                return trade
            quote = next(
                (
                    item
                    for item in snapshot.option_quotes
                    if item.side is trade.side and item.strike == trade.strike
                ),
                None,
            )
            if quote is None or quote.bid <= 0:
                raise PaperTradeRejected("No usable bid quote available for square-off.")
            result = self._costs.net_result(
                entry_price=trade.entry_price, exit_price=quote.bid,
                quantity=trade.quantity, underlying=trade.symbol,
            )
            trade.current_price = quote.bid
            trade.gross_pnl = result.gross_pnl
            trade.charges = result.costs
            self._close(trade, quote.bid, PaperTradeStatus.CLOSED, result.net_pnl)
            if self._journal is not None:
                self._journal.record_paper_trade(trade)
            if self._notifier is not None:
                self._notifier.notify_close(trade)
            return trade

    @staticmethod
    def _close(trade: PaperTrade, price: float, status: PaperTradeStatus, net_pnl: float) -> None:
        trade.status = status
        trade.exit_price = price
        trade.closed_at = datetime.now(timezone.utc)
        trade.realized_pnl = net_pnl
        trade.unrealized_pnl = 0.0
