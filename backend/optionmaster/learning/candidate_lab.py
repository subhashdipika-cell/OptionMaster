"""Bounded, reproducible strategy discovery for paper research.

This deliberately evaluates a small, declared candidate set using a
chronological holdout.  It is not a live execution authority and does not
promote a strategy merely because it wins an in-sample backtest.
"""

from datetime import date

from pydantic import BaseModel, Field

from optionmaster.backtest.reversal import ReversalParams
from optionmaster.backtest.runner import BacktestRun, BacktestRunRequest, BacktestRunSummary, run_backtest
from optionmaster.backtest.scalper import ScalpParams
from optionmaster.backtest.data import StoredDataRepository
from optionmaster.backtest.spot import SpotCandleRepository
from optionmaster.costs.calculator import NseOptionCostCalculator


class CandidateEvaluation(BaseModel):
    candidate_id: str
    label: str
    methodology: str
    in_sample: BacktestRunSummary
    holdout: BacktestRunSummary
    eligible_for_paper_trial: bool
    reasons: list[str] = Field(default_factory=list)


class CandidateLabReport(BaseModel):
    symbols: list[str]
    split_date: date | None
    candidates: list[CandidateEvaluation]
    evidence_rule: str = "Positive in-sample and holdout net P&L; holdout PF >= 1.15; at least 20 holdout trades."
    execution_rule: str = "Research only. A qualifying candidate still requires a separately tracked forward-paper trial."


class CandidateLab:
    """Runs a fixed, auditable parameter family against stored Dhan data."""

    min_holdout_trades = 20
    min_profit_factor = 1.15

    def __init__(self, *, repository: StoredDataRepository, spot_repository: SpotCandleRepository, calculator: NseOptionCostCalculator, lot_sizes: dict[str, int]) -> None:
        self._repository = repository
        self._spot_repository = spot_repository
        self._calculator = calculator
        self._lot_sizes = lot_sizes

    def run(self, symbols: list[str] | None = None) -> tuple[CandidateLabReport, list[BacktestRun]]:
        selected = [item.upper() for item in (symbols or ["NIFTY50", "BANKNIFTY"])]
        days = sorted({day for symbol, day in self._repository.list_days() if symbol in selected})
        if len(days) < 10:
            raise ValueError("At least 10 stored market sessions are needed for a chronological holdout.")
        split_index = max(1, int(len(days) * 0.70))
        split_date = days[split_index]
        candidates = self._candidates(selected)
        evaluations: list[CandidateEvaluation] = []
        runs: list[BacktestRun] = []
        for candidate_id, label, methodology, request in candidates:
            in_request = request.model_copy(update={"end_date": days[split_index - 1]})
            out_request = request.model_copy(update={"start_date": split_date})
            in_run = self._run(in_request)
            out_run = self._run(out_request)
            runs.extend((in_run, out_run))
            reasons = self._reasons(in_run.summary, out_run.summary)
            evaluations.append(CandidateEvaluation(
                candidate_id=candidate_id,
                label=label,
                methodology=methodology,
                in_sample=in_run.summary,
                holdout=out_run.summary,
                eligible_for_paper_trial=not reasons,
                reasons=reasons,
            ))
        return CandidateLabReport(symbols=selected, split_date=split_date, candidates=evaluations), runs

    def _run(self, request: BacktestRunRequest) -> BacktestRun:
        return run_backtest(
            request,
            repository=self._repository,
            spot_repository=self._spot_repository,
            calculator=self._calculator,
            lot_sizes=self._lot_sizes,
        )

    @staticmethod
    def _candidates(symbols: list[str]):
        return [
            ("momentum-balanced", "Momentum scalp — balanced", "Spot momentum plus option premium confirmation.", BacktestRunRequest(symbols=symbols, strategy="stored-scalp-v1", params=ScalpParams(strategy_id="momentum-balanced-v1"))),
            ("momentum-selective", "Momentum scalp — selective", "Stricter spot move and liquidity-oriented hold profile.", BacktestRunRequest(symbols=symbols, strategy="stored-scalp-v1", params=ScalpParams(strategy_id="momentum-selective-v1", momentum_threshold_pct=0.22, target_pct=18.0, max_hold_minutes=10.0, max_trades_per_day=3))),
            ("sweep-reversal", "Sweep reversal", "Closed-candle failed-breakdown/reclaim with option-premium fills.", BacktestRunRequest(symbols=symbols, strategy="stored-reversal-v1", reversal=ReversalParams(strategy_id="sweep-reversal-v1"))),
            ("sweep-selective", "Sweep reversal — selective", "Deeper liquidity sweep and stronger close reclaim.", BacktestRunRequest(symbols=symbols, strategy="stored-reversal-v1", reversal=ReversalParams(strategy_id="sweep-selective-v1", sweep_lookback_bars=20, min_sweep_pct=0.03, reclaim_pct=70.0, max_trades_per_day=3))),
        ]

    def _reasons(self, in_sample: BacktestRunSummary, holdout: BacktestRunSummary) -> list[str]:
        reasons: list[str] = []
        if in_sample.net_pnl <= 0:
            reasons.append("In-sample net P&L after costs is not positive.")
        if holdout.trades < self.min_holdout_trades:
            reasons.append(f"Need at least {self.min_holdout_trades} holdout trades; only {holdout.trades} were recorded.")
        if holdout.net_pnl <= 0:
            reasons.append("Chronological holdout net P&L after costs is not positive.")
        if holdout.profit_factor is None or holdout.profit_factor < self.min_profit_factor:
            reasons.append(f"Holdout profit factor must be at least {self.min_profit_factor:.2f}.")
        return reasons
