from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from optionmaster import __version__
from optionmaster.alerts.telegram import TelegramNotifier
from optionmaster.backtest.data import StoredDataRepository
from optionmaster.backtest.spot import SpotCandleRepository
from optionmaster.backtest.runner import (
    BacktestRun,
    BacktestRunOverview,
    BacktestRunRequest,
    StoredDataStatus,
    run_backtest,
    stored_data_status,
)
from optionmaster.config import get_settings
from optionmaster.context.engine import ContextEngine
from optionmaster.context.models import (
    ContextDecision,
    ContextEvaluationRequest,
    ContextOutcomeReport,
    ContextShadowSummary,
    ShadowAction,
)
from optionmaster.costs.calculator import NseOptionCostCalculator, NseOptionCostSchedule
from optionmaster.backtest.costs import BacktestOptionTradeRequest, evaluate_backtest_option_trade
from optionmaster.dhan.client import DhanClientFactory
from optionmaster.dhan.service import DhanMarketService
from optionmaster.execution.mode import (
    ExecutionMode,
    ExecutionModeChangeRequest,
    ExecutionModeState,
    ExecutionModeStore,
)
from optionmaster.execution.real_orders import DhanRealOrderClient, RealOrderRejected
from optionmaster.execution.super_order import SuperOrderIntent, SuperOrderPlanRequest, build_super_order_intent
from optionmaster.instruments.models import QuantityRequest
from optionmaster.instruments.scrip_master import NseScripMaster, ScripMasterError
from optionmaster.instruments.sizing import QuantityResolutionError, resolve_quantity
from optionmaster.learning.service import (
    AutoPromotionResult,
    LearningService,
    PaperPromotionRejected,
    PaperTrialRejected,
    ProfileEvaluation,
    StrategyProfileRegistry,
    UnknownStrategyProfile,
)
from optionmaster.learning.regime_router import RegimeStrategyRouter
from optionmaster.learning.candidate_lab import CandidateLab, CandidateLabReport
from optionmaster.market.features import build_features
from optionmaster.market.models import AnalysisResult, MarketSnapshot, OptionQuote, OptionSide, Regime, Signal
from optionmaster.market.regime_learning import RegimePerformanceReport
from optionmaster.journal.store import EntryAttempt, PerformanceSummary, RecordedBacktestResult, TradeJournal
from optionmaster.paper.broker import PaperBroker, PaperTradeRejected
from optionmaster.paper.models import CreatePaperTradeRequest, PaperTrade, PaperTradeDecision, RealTradeDecision
from optionmaster.ollama.models import OllamaReview, OllamaReviewSummary, OllamaStatus
from optionmaster.ollama.reviewer import OllamaReviewError, OllamaSetupReviewer
from optionmaster.risk.gate import apply_risk_gate
from optionmaster.scalping.dhan_stream import DhanScalpingFeed
from optionmaster.scalping.models import (
    BreakoutRetestPaperStartRequest, MarketTick, ScalpingDecision, ScalpingSession,
    ScalpingSessionRequest, ScalpingStrategy,
)
from optionmaster.scalping.session import ScalpingSessionManager
from optionmaster.strategy.profiles import StrategyProfile
from optionmaster.strategy.profiles import PAPER_ORB_VWAP_PROFILE
from optionmaster.strategy.orb_vwap_live import (
    OrbVwapGate,
    PremiumMomentumTracker,
    diagnose_m1_bars,
    evaluate_m1_bars,
)
from optionmaster.strategy.selector import select_signal

app = FastAPI(title="OptionMaster", version=__version__)
frontend_port = get_settings().frontend_port
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://127.0.0.1:{frontend_port}", f"http://localhost:{frontend_port}"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
cost_calculator = NseOptionCostCalculator(NseOptionCostSchedule.from_settings(get_settings()))
journal = TradeJournal(get_settings().journal_path)
strategy_profiles = StrategyProfileRegistry(journal)
learning_service = LearningService(journal=journal, profiles=strategy_profiles)
context_engine = ContextEngine()
telegram_notifier = TelegramNotifier(
    get_settings().telegram_bot_token, get_settings().telegram_chat_id
)
paper_broker = PaperBroker(
    cost_calculator=cost_calculator, journal=journal, notifier=telegram_notifier
)
ollama_reviewer = OllamaSetupReviewer(settings=get_settings(), journal=journal)
stored_data_repository = StoredDataRepository(get_settings().stored_data_dir)
spot_candle_repository = SpotCandleRepository(get_settings().spot_data_dir)
nse_scrip_master = NseScripMaster(get_settings().scrip_master_dir)
scalping_sessions = ScalpingSessionManager()
scalping_streams: dict[str, DhanScalpingFeed] = {}
execution_mode_store = ExecutionModeStore()
regime_router = RegimeStrategyRouter(journal=journal, profiles=strategy_profiles)
candidate_lab = CandidateLab(
    repository=stored_data_repository,
    spot_repository=spot_candle_repository,
    calculator=cost_calculator,
    lot_sizes=get_settings().lot_size_map,
)
orb_vwap_premium_tracker = PremiumMomentumTracker()


class EntryRejected(ValueError):
    def __init__(self, reason: str, analysis: AnalysisResult) -> None:
        super().__init__(reason)
        self.analysis = analysis


@dataclass(frozen=True, slots=True)
class PreparedEntry:
    request: CreatePaperTradeRequest
    analysis: AnalysisResult
    snapshot: MarketSnapshot
    signal: Signal
    quote: OptionQuote
    quantity: int
    lot_size: int
    contract_security_id: int
    strategy_id: str
    minimum_signal_score: float
    context_decision_id: str | None


def _record_live_scalping_tick(session_id: str, tick: MarketTick) -> None:
    """Apply streamed data to the paper-only engine and retain actionable evidence."""
    try:
        decision = scalping_sessions.ingest(session_id, tick)
        journal.record_scalping_decision(session_id, decision)
    except (KeyError, ValueError) as exc:
        # A stream failure must never be interpreted as permission to trade.
        scalping_sessions.set_running(session_id, False, str(exc))


def _record_shadow_context(
    *,
    service: DhanMarketService,
    snapshot: MarketSnapshot,
    signal: Signal,
    security_id: int,
    segment: str,
    instrument_type: str,
) -> ContextDecision | None:
    """Run structure filters beside the strategy; never change an execution decision here."""
    try:
        bars = service.intraday_bars(
            security_id=security_id,
            segment=segment,
            instrument_type=instrument_type,
        )
        if len(bars) < 2:
            return None
        decision = context_engine.evaluate(
            ContextEvaluationRequest(snapshot=snapshot, signal=signal, bars=bars)
        )
        journal.record_context_decision(decision)
        return decision
    except Exception:
        # Shadow context must not make a data analysis endpoint unavailable.
        return None


def _evaluate_routed_market(
    snapshot: MarketSnapshot,
    *,
    risk_gate: bool,
    routing_execution_mode: ExecutionMode | None = None,
) -> tuple[AnalysisResult, object]:
    """Classify the market, retain the evidence, then select its paper strategy."""
    features = build_features(snapshot)
    selection = regime_router.select(
        features=features,
        execution_mode=routing_execution_mode or execution_mode_store.get().mode,
    )
    journal.record_regime_observation(selection.observation)
    signal = select_signal(snapshot, features, selection.regime, selection.profile)
    if risk_gate:
        signal = apply_risk_gate(signal, minimum_signal_score=selection.profile.risk_gate_minimum_score)
    return AnalysisResult(snapshot=snapshot, features=features, signal=signal), selection.profile


def _prepare_option_entry(
    request: CreatePaperTradeRequest,
    *,
    routing_execution_mode: ExecutionMode | None = None,
) -> PreparedEntry:
    """Fetch, analyse and validate a contract without placing an order."""
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    service = DhanMarketService(DhanClientFactory(settings))
    snapshot = service.live_snapshot(
        symbol=request.symbol,
        security_id=request.security_id,
        segment=request.segment,
        expiry=request.expiry,
        instrument_type=request.instrument_type,
        vix_security_id=settings.india_vix_security_id,
        vix_segment=settings.india_vix_segment,
        vix_instrument_type=settings.india_vix_instrument_type,
    )
    analysis, profile = _evaluate_routed_market(
        snapshot, risk_gate=False, routing_execution_mode=routing_execution_mode
    )
    effective_request = request
    if profile.paper_trial_only:
        # The paper trade, stop, target, and risk check must all match the
        # 10% / 15% configuration that was tested historically.
        effective_request = request.model_copy(update={
            "stop_loss_fraction": profile.paper_stop_loss_fraction,
            "target_fraction": profile.paper_target_fraction,
        })
    signal = analysis.signal
    context_decision = _record_shadow_context(
        service=service, snapshot=snapshot, signal=signal,
        security_id=request.security_id, segment=request.segment,
        instrument_type=request.instrument_type,
    )
    if context_decision is not None and context_decision.action is ShadowAction.WOULD_SKIP:
        raise EntryRejected(
            "Index-chart context gate rejected the setup: " + "; ".join(context_decision.reasons),
            analysis,
        )
    if signal.side is None or signal.strike is None:
        raise EntryRejected(f"No trade signal: {signal.reason}", analysis)
    # The local model receives this setup in the background. Its verdict never
    # changes the deterministic signal, sizing, or order path.
    ollama_reviewer.review_async(analysis=analysis, context=context_decision)
    quote = next(
        (item for item in snapshot.option_quotes if item.side is signal.side and item.strike == signal.strike),
        None,
    )
    if quote is None:
        raise EntryRejected(
            f"Signalled contract {signal.strike} {signal.side} is not in the Dhan chain ({len(snapshot.option_quotes)} quotes).",
            analysis,
        )
    if quote.security_id is None:
        raise EntryRejected("The selected contract has no Dhan security ID for lot-size resolution.", analysis)
    if signal.entry_price and signal.stop_loss_price and signal.target_price:
        effective_request = effective_request.model_copy(update={
            "stop_loss_fraction": max(0.001, (signal.entry_price - signal.stop_loss_price) / signal.entry_price),
            "target_fraction": max(0.001, (signal.target_price - signal.entry_price) / signal.entry_price),
        })
    try:
        # This auto-loads or refreshes the current NSE lot-size master before
        # every orderable entry, rather than rejecting a valid setup on a new install.
        nse_scrip_master.ensure_current()
        quantity = resolve_quantity(
            scrip_master=nse_scrip_master, security_id=quote.security_id,
            lots=request.lots, requested_quantity=request.quantity,
        )
    except (ScripMasterError, QuantityResolutionError) as exc:
        raise EntryRejected(str(exc), analysis) from exc

    try:
        paper_broker.entry_risk_check(request=effective_request, quote=quote, quantity=quantity.quantity)
    except PaperTradeRejected as primary_rejection:
        # The score-first selector naturally favours a higher-delta ATM quote.
        # When one whole exchange lot breaches the fixed risk budget, retain
        # direction and liquidity requirements but seek a lower-premium,
        # same-side contract before rejecting the setup altogether.
        alternatives = sorted(
            (
                candidate for candidate in snapshot.option_quotes
                if candidate is not quote
                and candidate.side is signal.side
                and candidate.security_id is not None
                and candidate.ask > 0 and candidate.bid > 0
                and profile.minimum_option_delta <= abs(candidate.delta) <= profile.maximum_option_delta
                and candidate.spread_fraction <= profile.maximum_option_spread_fraction
            ),
            key=lambda candidate: (candidate.ask, -abs(candidate.delta)),
        )
        selected = None
        for candidate in alternatives:
            try:
                candidate_quantity = resolve_quantity(
                    scrip_master=nse_scrip_master, security_id=candidate.security_id,
                    lots=request.lots, requested_quantity=request.quantity,
                )
                paper_broker.entry_risk_check(
                    request=effective_request, quote=candidate, quantity=candidate_quantity.quantity
                )
            except (PaperTradeRejected, QuantityResolutionError):
                continue
            selected = candidate, candidate_quantity
            break
        if selected is None:
            raise EntryRejected(str(primary_rejection), analysis) from primary_rejection
        quote, quantity = selected
        signal.strike = quote.strike
        signal.reason = f"{signal.reason} Selected a lower-premium risk-fit {quote.side} contract."
    return PreparedEntry(
        request=effective_request, analysis=analysis, snapshot=snapshot, signal=signal,
        quote=quote, quantity=quantity.quantity, lot_size=quantity.lot_size,
        contract_security_id=quote.security_id, strategy_id=profile.id,
        minimum_signal_score=profile.minimum_signal_score,
        context_decision_id=context_decision.id if context_decision is not None else None,
    )


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    mode = execution_mode_store.get()
    return {
        "status": "ok",
        "application": "OptionMaster",
        "version": __version__,
        "execution_mode": mode.mode.value,
        "dhan_configured": settings.dhan_configured,
        "underlyings": settings.underlying_symbols,
    }


@app.post("/api/v1/analyze", response_model=Signal)
def analyze(snapshot: MarketSnapshot) -> Signal:
    """Analyze a supplied snapshot without placing an order."""
    analysis, _ = _evaluate_routed_market(snapshot, risk_gate=True)
    return analysis.signal


@app.get("/api/v1/dhan/status")
def dhan_status() -> dict[str, object]:
    settings = get_settings()
    mode = execution_mode_store.get()
    return {
        "configured": settings.dhan_configured,
        "execution_mode": mode.mode.value,
        "live_orders_enabled": mode.mode is ExecutionMode.REAL,
        "data_access": "available when credentials are configured",
    }


@app.get("/api/v1/ollama/status", response_model=OllamaStatus)
def ollama_status() -> OllamaStatus:
    return ollama_reviewer.status()


@app.get("/api/v1/ollama/reviews", response_model=list[OllamaReview])
def ollama_reviews(limit: int = Query(default=20, ge=1, le=200)) -> list[OllamaReview]:
    return journal.list_ollama_reviews(limit)


@app.get("/api/v1/reports/ollama", response_model=OllamaReviewSummary)
def ollama_report() -> OllamaReviewSummary:
    return journal.ollama_review_summary()


@app.get("/api/v1/execution/mode", response_model=ExecutionModeState)
def execution_mode_status() -> ExecutionModeState:
    return execution_mode_store.get()


@app.post("/api/v1/execution/mode", response_model=ExecutionModeState)
def set_execution_mode(request: ExecutionModeChangeRequest) -> ExecutionModeState:
    if request.mode is ExecutionMode.REAL and strategy_profiles.active().paper_trial_only:
        raise HTTPException(
            status_code=409,
            detail="End the active paper-only strategy trial before arming Real Trade.",
        )
    try:
        return execution_mode_store.set(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/instruments/nse/status")
def nse_scrip_master_status() -> dict[str, object]:
    """Show whether a local copy of Dhan's NSE scrip master is available."""
    summary = nse_scrip_master.status()
    return {"available": summary is not None, "summary": summary.as_dict() if summary else None}


@app.post("/api/v1/instruments/nse/refresh")
def refresh_nse_scrip_master() -> dict[str, object]:
    """Download the current Dhan detailed master and retain NSE SEM_LOT_UNITS values."""
    try:
        summary = nse_scrip_master.refresh()
    except ScripMasterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"refreshed": True, "summary": summary.as_dict()}


@app.post("/api/v1/backtests/resolve-quantity")
def resolve_backtest_quantity(request: QuantityRequest) -> dict[str, int]:
    """Resolve an order quantity from the same master used by forward paper tests."""
    try:
        return resolve_quantity(
            scrip_master=nse_scrip_master,
            security_id=request.contract_security_id,
            lots=request.lots,
            requested_quantity=request.quantity,
        ).as_dict()
    except QuantityResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/backtests/evaluate-option-trade")
def evaluate_backtest_trade(request: BacktestOptionTradeRequest):
    """Return gross P&L, the full estimated cost breakdown, and net P&L."""
    return evaluate_backtest_option_trade(request, calculator=cost_calculator)


@app.post("/api/v1/backtests/record-option-trade", response_model=RecordedBacktestResult)
def record_backtest_trade(request: BacktestOptionTradeRequest) -> RecordedBacktestResult:
    """Evaluate and retain a net-of-cost historical option trade."""
    result = evaluate_backtest_option_trade(request, calculator=cost_calculator)
    return journal.record_backtest_trade(result, strategy_id=request.strategy_id)


@app.get("/api/v1/backtests/data-status", response_model=StoredDataStatus)
def backtest_data_status() -> StoredDataStatus:
    """Describe the locally stored Dhan option-chain snapshot archive."""
    return stored_data_status(stored_data_repository)


@app.post("/api/v1/backtests/run", response_model=BacktestRun)
def run_stored_data_backtest(request: BacktestRunRequest) -> BacktestRun:
    """Replay the selected strategy over the stored Dhan snapshots and save the run."""
    if not stored_data_repository.available:
        raise HTTPException(
            status_code=503,
            detail=f"Stored data directory is not available: {get_settings().stored_data_dir}",
        )
    if request.strategy == "stored-reversal-v1" and not spot_candle_repository.available:
        raise HTTPException(
            status_code=503,
            detail=(
                "stored-reversal-v1 needs M1 index candles, but the spot data "
                f"directory is not available: {get_settings().spot_data_dir}"
            ),
        )
    run = run_backtest(
        request,
        repository=stored_data_repository,
        calculator=cost_calculator,
        lot_sizes=get_settings().lot_size_map,
        spot_repository=spot_candle_repository,
    )
    journal.record_backtest_run(run)
    return run


@app.get("/api/v1/backtests/runs", response_model=list[BacktestRunOverview])
def list_backtest_runs() -> list[BacktestRunOverview]:
    return [
        BacktestRunOverview.model_validate_json(payload)
        for payload in journal.list_backtest_runs()
    ]


@app.get("/api/v1/backtests/runs/{run_id}", response_model=BacktestRun)
def get_backtest_run(run_id: str) -> BacktestRun:
    payload = journal.get_backtest_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    return BacktestRun.model_validate_json(payload)


@app.get("/api/v1/alerts/status")
def alerts_status() -> dict[str, object]:
    return {
        "telegram_configured": telegram_notifier.configured,
        "events": ["position opened", "position closed"],
    }


@app.post("/api/v1/alerts/test")
def send_test_alert() -> dict[str, object]:
    """Send a synchronous test message so configuration problems surface immediately."""
    if not telegram_notifier.configured:
        raise HTTPException(
            status_code=503,
            detail="Set OPTIONMASTER_TELEGRAM_BOT_TOKEN and OPTIONMASTER_TELEGRAM_CHAT_ID first.",
        )
    delivered = telegram_notifier.send_text(
        "🔔 OptionMaster test alert — position open/close notifications are working.",
        wait=True,
    )
    if not delivered:
        raise HTTPException(status_code=502, detail="Telegram rejected the test message; check token and chat id.")
    return {"delivered": True}


@app.get("/api/v1/reports/paper-performance", response_model=PerformanceSummary)
def paper_performance(strategy_id: str | None = Query(default=None, min_length=3)) -> PerformanceSummary:
    return journal.performance("paper", strategy_id=strategy_id)


@app.get("/api/v1/reports/backtest-performance", response_model=PerformanceSummary)
def backtest_performance(strategy_id: str | None = Query(default=None, min_length=3)) -> PerformanceSummary:
    return journal.performance("backtest", strategy_id=strategy_id)


@app.get("/api/v1/reports/regime-performance", response_model=RegimePerformanceReport)
def regime_performance_report() -> RegimePerformanceReport:
    """Show current regime, route choice, and closed-paper evidence by strategy and regime."""
    return regime_router.report()


@app.post("/api/v1/research/candidates/run", response_model=CandidateLabReport)
def run_candidate_research() -> CandidateLabReport:
    """Run fixed candidates with a chronological holdout; never changes execution."""
    if execution_mode_store.get().mode is not ExecutionMode.PAPER:
        raise HTTPException(status_code=409, detail="Candidate research is locked outside PAPER mode.")
    if not stored_data_repository.available or not spot_candle_repository.available:
        raise HTTPException(status_code=503, detail="Stored option and M1 spot data are required for candidate research.")
    try:
        report, runs = candidate_lab.run()
        for run in runs:
            journal.record_backtest_run(run)
        return report
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/reports/context-shadow", response_model=ContextShadowSummary)
def context_shadow_report() -> ContextShadowSummary:
    """Summarize the observation-only filters across all saved signal evaluations."""
    return journal.context_shadow_summary()


@app.get("/api/v1/reports/context-outcomes", response_model=ContextOutcomeReport)
def context_outcome_report() -> ContextOutcomeReport:
    """Compare closed paper P&L with the context features captured before entry."""
    return journal.context_outcome_report()


@app.post("/api/v1/context/evaluate", response_model=ContextDecision)
def evaluate_context(request: ContextEvaluationRequest) -> ContextDecision:
    """Evaluate supplied OHLCV and option-chain context; it cannot place or suppress an order."""
    decision = context_engine.evaluate(request)
    journal.record_context_decision(decision)
    return decision


@app.get("/api/v1/learning/profiles", response_model=list[StrategyProfile])
def list_learning_profiles() -> list[StrategyProfile]:
    """List the deterministic rule sets OptionMaster can evaluate in paper mode."""
    return strategy_profiles.list()


@app.get("/api/v1/learning/active-profile", response_model=StrategyProfile)
def active_learning_profile() -> StrategyProfile:
    return strategy_profiles.active()


@app.get("/api/v1/learning/profiles/{profile_id}/evaluation", response_model=ProfileEvaluation)
def evaluate_learning_profile(profile_id: str) -> ProfileEvaluation:
    try:
        return learning_service.evaluate(profile_id)
    except UnknownStrategyProfile as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/learning/profiles/{profile_id}/activate", response_model=StrategyProfile)
def activate_learning_profile(profile_id: str) -> StrategyProfile:
    """Activate only the baseline or a candidate that cleared the paper-evidence policy."""
    if get_settings().execution_mode != "PAPER":
        raise HTTPException(status_code=409, detail="Strategy profile changes are locked outside PAPER mode.")
    try:
        return learning_service.activate_if_eligible(profile_id)
    except UnknownStrategyProfile as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaperPromotionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/learning/profiles/{profile_id}/start-paper-trial", response_model=StrategyProfile)
def start_learning_paper_trial(profile_id: str) -> StrategyProfile:
    """Start a designated trial only while the application is in Paper mode."""
    if execution_mode_store.get().mode is not ExecutionMode.PAPER:
        raise HTTPException(status_code=409, detail="Switch to Paper Trade before starting a paper trial.")
    try:
        return strategy_profiles.start_paper_trial(profile_id)
    except UnknownStrategyProfile as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaperTrialRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/learning/end-paper-trial", response_model=StrategyProfile)
def end_learning_paper_trial() -> StrategyProfile:
    """Return to the baseline profile after an explicit paper-only trial."""
    if execution_mode_store.get().mode is not ExecutionMode.PAPER:
        raise HTTPException(status_code=409, detail="Switch to Paper Trade before ending a paper trial.")
    if not strategy_profiles.active().paper_trial_only:
        raise HTTPException(status_code=409, detail="There is no active paper-only strategy trial to end.")
    return strategy_profiles.activate("baseline-v1")


@app.post("/api/v1/learning/review-and-promote", response_model=AutoPromotionResult)
def review_and_promote_learning_profile() -> AutoPromotionResult:
    """Select the strongest qualifying candidate using closed forward paper results only."""
    if get_settings().execution_mode != "PAPER":
        raise HTTPException(status_code=409, detail="Automated learning changes are locked outside PAPER mode.")
    return learning_service.review_and_promote()


@app.post("/api/v1/scalping/sessions", response_model=ScalpingSession)
def create_scalping_session(request: ScalpingSessionRequest) -> ScalpingSession:
    """Create a paper-first dual-stream scalping session; it does not start a feed yet."""
    try:
        resolved = resolve_quantity(
            scrip_master=nse_scrip_master,
            security_id=request.option_security_id,
            lots=1,
        )
    except QuantityResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if request.lot_size is not None and request.lot_size != resolved.lot_size:
        raise HTTPException(
            status_code=409,
            detail=f"Requested lot size {request.lot_size} differs from current Dhan SEM_LOT_UNITS {resolved.lot_size}.",
        )
    configuration = request.model_copy(update={"lot_size": resolved.lot_size})
    return scalping_sessions.create(configuration)


@app.post("/api/v1/scalping/breakout-retest/start-paper", response_model=ScalpingSession)
def start_breakout_retest_paper_monitor(request: BreakoutRetestPaperStartRequest) -> ScalpingSession:
    """Subscribe to one current ATM option for a 15-second, paper-only monitor."""
    if execution_mode_store.get().mode is not ExecutionMode.PAPER:
        raise HTTPException(status_code=409, detail="Breakout-retest monitoring is available only in Paper Trade mode.")
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    service = DhanMarketService(DhanClientFactory(settings))
    try:
        expiry = request.expiry.isoformat() if request.expiry else None
        if expiry is None:
            from datetime import date
            candidates = sorted(item for item in service.expiries(settings.auto_security_id, settings.auto_segment) if item > date.today().isoformat())
            expiry = candidates[0] if candidates else None
        if expiry is None:
            raise ValueError("No non-expiry-day contract is available for the paper monitor.")
        snapshot = service.option_chain_snapshot(
            symbol=settings.auto_symbol, security_id=settings.auto_security_id,
            segment=settings.auto_segment, expiry=expiry,
        )
        candidates = [
            quote for quote in snapshot.option_quotes
            if quote.side is request.option_side and quote.security_id is not None and quote.bid > 0 and quote.ask > 0
        ]
        if not candidates:
            raise ValueError(f"Dhan returned no usable {request.option_side} contract for the selected expiry.")
        candidates = [
            quote for quote in candidates
            if 0.25 <= abs(quote.delta) <= 0.65
            and quote.spread_fraction <= 0.03
            and quote.oi > 0 and quote.volume > 0
        ]
        if not candidates:
            raise ValueError("Dhan returned no liquid Greek/OI-qualified contract for the selected option side.")
        option = max(
            candidates,
            key=lambda quote: (
                min(abs(quote.delta) / 0.55, 1.0),
                min(abs(quote.oi_change) / max(quote.oi, 1.0) * 10.0, 1.0),
                min(quote.volume / max(quote.oi, 1.0) * 100.0, 1.0),
                -abs(quote.strike - snapshot.underlying),
            ),
        )
        nse_scrip_master.ensure_current()
        resolved = resolve_quantity(scrip_master=nse_scrip_master, security_id=option.security_id, lots=1)
        configuration = ScalpingSessionRequest(
            symbol=settings.auto_symbol, spot_security_id=settings.auto_security_id,
            option_security_id=option.security_id, option_side=request.option_side,
            expiry=expiry, spot_segment=settings.auto_segment, option_segment="NSE_FNO",
            lot_size=resolved.lot_size, strategy=ScalpingStrategy.BREAKOUT_RETEST_3_BAR,
        )
        session = scalping_sessions.create(configuration)
        feed = DhanScalpingFeed(
            settings=settings, configuration=configuration,
            on_tick=lambda tick: _record_live_scalping_tick(session.id, tick),
            on_error=lambda error: scalping_sessions.set_running(session.id, False, error),
        )
        feed.start()
        scalping_streams[session.id] = feed
        return scalping_sessions.set_running(session.id, True)
    except (RuntimeError, ValueError, QuantityResolutionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/scalping/sessions/{session_id}", response_model=ScalpingSession)
def get_scalping_session(session_id: str) -> ScalpingSession:
    session = scalping_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Scalping session not found.")
    return session


@app.post("/api/v1/scalping/sessions/{session_id}/replay-tick", response_model=ScalpingDecision)
def replay_scalping_tick(session_id: str, tick: MarketTick) -> ScalpingDecision:
    """Feed a recorded tick into the identical engine used by the live stream."""
    try:
        decision = scalping_sessions.ingest(session_id, tick)
        journal.record_scalping_decision(session_id, decision)
        return decision
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scalping session not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/scalping/sessions/{session_id}/start", response_model=ScalpingSession)
def start_scalping_session(session_id: str) -> ScalpingSession:
    """Start Dhan's read-only spot-plus-option Full-feed subscription."""
    settings = get_settings()
    session = scalping_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Scalping session not found.")
    if session.running:
        return session
    try:
        feed = DhanScalpingFeed(
            settings=settings,
            configuration=session.configuration,
            on_tick=lambda tick: _record_live_scalping_tick(session_id, tick),
            on_error=lambda error: scalping_sessions.set_running(session_id, False, error),
        )
        feed.start()
        scalping_streams[session_id] = feed
        return scalping_sessions.set_running(session_id, True)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/scalping/sessions/{session_id}/stop", response_model=ScalpingSession)
def stop_scalping_session(session_id: str) -> ScalpingSession:
    feed = scalping_streams.pop(session_id, None)
    session = scalping_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Scalping session not found.")
    if feed is not None:
        feed.stop()
    return scalping_sessions.set_running(session_id, False)


@app.post("/api/v1/scalping/sessions/{session_id}/super-order-intent", response_model=SuperOrderIntent)
def scalping_super_order_intent(session_id: str, request: SuperOrderPlanRequest) -> SuperOrderIntent:
    """Build a Dhan Super Order payload for review. No broker request is sent."""
    session = scalping_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Scalping session not found.")
    if session.latest_decision is None:
        raise HTTPException(status_code=409, detail="No scalping decision is available yet.")
    if request.contract_security_id != session.configuration.option_security_id:
        raise HTTPException(status_code=409, detail="Super Order contract does not match the scalping session option.")
    try:
        quantity = resolve_quantity(
            scrip_master=nse_scrip_master,
            security_id=request.contract_security_id,
            lots=request.lots,
            requested_quantity=request.quantity,
        )
        plan = request.model_copy(update={"quantity": quantity.quantity})
        return build_super_order_intent(session.latest_decision, plan, calculator=cost_calculator)
    except (QuantityResolutionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/dhan/expiries")
def dhan_expiries(
    security_id: int = Query(gt=0),
    segment: str = Query(min_length=1),
) -> dict[str, object]:
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        expiries = DhanMarketService(DhanClientFactory(settings)).expiries(security_id, segment)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan data request failed: {exc}") from exc
    return {"security_id": security_id, "segment": segment, "expiries": expiries}


@app.get("/api/v1/dhan/option-chain", response_model=MarketSnapshot)
def dhan_option_chain(
    symbol: str = Query(min_length=1),
    security_id: int = Query(gt=0),
    segment: str = Query(min_length=1),
    expiry: str = Query(min_length=10, max_length=10),
) -> MarketSnapshot:
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        return DhanMarketService(DhanClientFactory(settings)).live_snapshot(
            symbol=symbol,
            security_id=security_id,
            segment=segment,
            expiry=expiry,
            vix_security_id=settings.india_vix_security_id,
            vix_segment=settings.india_vix_segment,
            vix_instrument_type=settings.india_vix_instrument_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan option-chain request failed: {exc}") from exc


@app.get("/api/v1/dhan/analyze", response_model=AnalysisResult)
def dhan_analyze(
    symbol: str = Query(min_length=1),
    security_id: int = Query(gt=0),
    segment: str = Query(min_length=1),
    expiry: str = Query(min_length=10, max_length=10),
    instrument_type: str = Query(default="INDEX", min_length=1),
) -> AnalysisResult:
    """Fetch live Dhan data and return a paper-only trading analysis."""
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        service = DhanMarketService(DhanClientFactory(settings))
        snapshot = service.live_snapshot(
            symbol=symbol,
            security_id=security_id,
            segment=segment,
            expiry=expiry,
            instrument_type=instrument_type,
            vix_security_id=settings.india_vix_security_id,
            vix_segment=settings.india_vix_segment,
            vix_instrument_type=settings.india_vix_instrument_type,
        )
        analysis, _ = _evaluate_routed_market(snapshot, risk_gate=True)
        signal = analysis.signal
        _record_shadow_context(
            service=service,
            snapshot=snapshot,
            signal=signal,
            security_id=security_id,
            segment=segment,
            instrument_type=instrument_type,
        )
        return analysis
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan analysis request failed: {exc}") from exc


@app.get("/api/v1/dhan/context", response_model=ContextDecision)
def dhan_context(
    symbol: str = Query(min_length=1),
    security_id: int = Query(gt=0),
    segment: str = Query(min_length=1),
    expiry: str = Query(min_length=10, max_length=10),
    instrument_type: str = Query(default="INDEX", min_length=1),
) -> ContextDecision:
    """Fetch Dhan data and run the observation-only freshness and structure assessment."""
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        service = DhanMarketService(DhanClientFactory(settings))
        snapshot = service.live_snapshot(
            symbol=symbol,
            security_id=security_id,
            segment=segment,
            expiry=expiry,
            instrument_type=instrument_type,
            vix_security_id=settings.india_vix_security_id,
            vix_segment=settings.india_vix_segment,
            vix_instrument_type=settings.india_vix_instrument_type,
        )
        analysis, _ = _evaluate_routed_market(snapshot, risk_gate=True)
        signal = analysis.signal
        decision = _record_shadow_context(
            service=service,
            snapshot=snapshot,
            signal=signal,
            security_id=security_id,
            segment=segment,
            instrument_type=instrument_type,
        )
        if decision is None:
            raise RuntimeError("Dhan returned too little OHLCV data for a context evaluation.")
        return decision
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan context request failed: {exc}") from exc


@app.get("/api/v1/paper-trades", response_model=list[PaperTrade])
def paper_trades() -> list[PaperTrade]:
    """Return the durable paper-trade journal, including prior application runs."""
    return journal.list_paper_trades()


@app.post("/api/v1/dhan/paper-trades", response_model=PaperTradeDecision)
def create_dhan_paper_trade(request: CreatePaperTradeRequest) -> PaperTradeDecision:
    """Create a simulated option-buying trade only when the full gate passes."""
    prepared: PreparedEntry | None = None
    try:
        prepared = _prepare_option_entry(request, routing_execution_mode=ExecutionMode.PAPER)
        trade = paper_broker.open_from_signal(
            request=prepared.request, signal=prepared.signal, snapshot=prepared.snapshot,
            quantity=prepared.quantity, lot_size=prepared.lot_size,
            contract_security_id=prepared.contract_security_id,
            strategy_id=prepared.strategy_id,
            minimum_signal_score=prepared.minimum_signal_score,
            context_decision_id=prepared.context_decision_id,
        )
        return PaperTradeDecision(accepted=True, reason="Paper trade opened.", analysis=prepared.analysis, trade=trade)
    except EntryRejected as exc:
        return PaperTradeDecision(accepted=False, reason=str(exc), analysis=exc.analysis)
    except PaperTradeRejected as exc:
        # The analysis was completed but the paper risk gate blocked the fill.
        assert prepared is not None
        return PaperTradeDecision(accepted=False, reason=str(exc), analysis=prepared.analysis)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan paper-trade request failed: {exc}") from exc


def create_orb_vwap_paper_trade(request: CreatePaperTradeRequest) -> PaperTradeDecision:
    """Evaluate the fixed ORB/VWAP strategy and, only in its paper trial, simulate it."""
    if execution_mode_store.get().mode is not ExecutionMode.PAPER:
        raise RuntimeError("ORB/VWAP forward trial is paper-only.")
    if strategy_profiles.active().id != PAPER_ORB_VWAP_PROFILE.id:
        raise RuntimeError("ORB/VWAP forward trial is not the active paper profile.")
    settings = get_settings()
    service = DhanMarketService(DhanClientFactory(settings))
    snapshot = service.live_snapshot(
        symbol=request.symbol, security_id=request.security_id, segment=request.segment,
        expiry=request.expiry, instrument_type=request.instrument_type,
        vix_security_id=settings.india_vix_security_id, vix_segment=settings.india_vix_segment,
        vix_instrument_type=settings.india_vix_instrument_type,
    )
    bars = service.intraday_bars(
        security_id=request.security_id, segment=request.segment,
        instrument_type=request.instrument_type, interval=1, lookback_days=1, maximum_bars=450,
    )
    setup = evaluate_m1_bars(bars)
    features = build_features(snapshot)
    if setup is None:
        orb_vwap_premium_tracker.observe(snapshot)
        signal = Signal(
            symbol=request.symbol.upper(), timestamp=snapshot.timestamp, regime=Regime.NO_TRADE,
            strategy_id=PAPER_ORB_VWAP_PROFILE.id, reason="ORB/VWAP: no completed qualifying setup.",
        )
        return PaperTradeDecision(
            accepted=False, reason=signal.reason,
            analysis=AnalysisResult(snapshot=snapshot, features=features, signal=signal),
        )
    quote = min(
        (item for item in snapshot.option_quotes if item.side is setup.side),
        key=lambda item: abs(item.strike - snapshot.underlying), default=None,
    )
    regime = Regime.BULLISH_TREND if setup.side is OptionSide.CE else Regime.BEARISH_TREND
    signal = Signal(
        symbol=request.symbol.upper(), timestamp=snapshot.timestamp, regime=regime,
        strategy_id=PAPER_ORB_VWAP_PROFILE.id, side=setup.side,
        strike=quote.strike if quote is not None else None, score=1.0,
        reason=(f"ORB/VWAP {setup.side.value}: opening range, VWAP, volume and ADX "
                f"{setup.adx:.1f} confirmed on the {setup.signal_bar_closed_at:%H:%M} IST bar."),
    )
    analysis = AnalysisResult(snapshot=snapshot, features=features, signal=signal)
    if quote is None or quote.ask <= 0 or quote.bid <= 0 or quote.security_id is None:
        orb_vwap_premium_tracker.observe(snapshot)
        return PaperTradeDecision(accepted=False, reason="ORB/VWAP: no usable ATM option quote.", analysis=analysis)
    if quote.ltp < 20 or quote.ask > 600 or quote.spread_fraction > 0.01:
        orb_vwap_premium_tracker.observe(snapshot)
        return PaperTradeDecision(accepted=False, reason="ORB/VWAP: option premium or spread filter blocked entry.", analysis=analysis)
    premium_change = orb_vwap_premium_tracker.observe_and_change(
        snapshot, strike=quote.strike, side=setup.side,
    )
    if premium_change is None or premium_change < 0.15:
        return PaperTradeDecision(
            accepted=False, reason="ORB/VWAP: awaiting same-direction option premium momentum.", analysis=analysis,
        )
    try:
        nse_scrip_master.ensure_current()
        quantity = resolve_quantity(
            scrip_master=nse_scrip_master, security_id=quote.security_id,
            lots=request.lots, requested_quantity=request.quantity,
        )
        context = _record_shadow_context(
            service=service, snapshot=snapshot, signal=signal, security_id=request.security_id,
            segment=request.segment, instrument_type=request.instrument_type,
        )
        if context is not None and context.action is ShadowAction.WOULD_SKIP:
            return PaperTradeDecision(
                accepted=False,
                reason="ORB/VWAP: index-chart context gate rejected the setup: " + "; ".join(context.reasons),
                analysis=analysis,
            )
        effective_request = request.model_copy(update={
            "stop_loss_fraction": 0.05, "target_fraction": 0.10,
        })
        trade = paper_broker.open_from_signal(
            request=effective_request, signal=signal, snapshot=snapshot,
            quantity=quantity.quantity, lot_size=quantity.lot_size,
            contract_security_id=quote.security_id, strategy_id=PAPER_ORB_VWAP_PROFILE.id,
            minimum_signal_score=1.0, context_decision_id=context.id if context else None,
        )
        return PaperTradeDecision(
            accepted=True, reason="ORB/VWAP paper trade opened.", analysis=analysis, trade=trade,
        )
    except (ScripMasterError, QuantityResolutionError, PaperTradeRejected) as exc:
        return PaperTradeDecision(accepted=False, reason=f"ORB/VWAP: {exc}", analysis=analysis)


@app.get("/api/v1/research/orb-vwap/diagnostics")
def orb_vwap_diagnostics() -> dict[str, object]:
    """Expose the live paper trial's gates without placing or changing an order."""
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        service = DhanMarketService(DhanClientFactory(settings))
        from datetime import date
        candidates = sorted(
            item for item in service.expiries(settings.auto_security_id, settings.auto_segment)
            if item > date.today().isoformat()
        )
        expiry = candidates[0] if candidates else None
        if expiry is None:
            raise RuntimeError("No non-expiry-day contract is available.")
        snapshot = service.live_snapshot(
            symbol=settings.auto_symbol, security_id=settings.auto_security_id,
            segment=settings.auto_segment, expiry=expiry,
            instrument_type=settings.auto_instrument_type,
            vix_security_id=settings.india_vix_security_id,
            vix_segment=settings.india_vix_segment,
            vix_instrument_type=settings.india_vix_instrument_type,
        )
        bars = service.intraday_bars(
            security_id=settings.auto_security_id, segment=settings.auto_segment,
            instrument_type=settings.auto_instrument_type, interval=1,
            lookback_days=1, maximum_bars=450,
        )
        diagnostic = diagnose_m1_bars(bars)
        gates = list(diagnostic.gates)
        option_detail: dict[str, object] = {"status": "Waiting for a structural setup."}
        ready = diagnostic.ready
        if diagnostic.candidate_side is not None:
            quote = min(
                (item for item in snapshot.option_quotes if item.side is diagnostic.candidate_side),
                key=lambda item: abs(item.strike - snapshot.underlying), default=None,
            )
            if quote is None:
                gates.append(OrbVwapGate(
                    key="liquidity", label="ATM option liquidity", passed=False,
                    detail="No current ATM quote.",
                ))
                gates.append(OrbVwapGate(
                    key="premium", label="Option premium momentum", passed=None,
                    detail="Waiting for a usable ATM quote.",
                ))
                ready = False
            else:
                liquid = quote.ltp >= 20 and quote.ask > 0 and quote.ask <= 600 and quote.bid > 0 and quote.spread_fraction <= 0.01
                spread_pct = quote.spread_fraction * 100
                gates.append(OrbVwapGate(
                    key="liquidity", label="ATM option liquidity", passed=liquid,
                    detail=(f"{quote.strike:.0f} {quote.side.value}: LTP ₹{quote.ltp:.2f}; "
                            f"spread {spread_pct:.2f}% (need ≤ 1.00%)."),
                ))
                change = orb_vwap_premium_tracker.latest_change(
                    snapshot, strike=quote.strike, side=quote.side,
                )
                momentum_ok = change is not None and change >= 0.15
                gates.append(OrbVwapGate(
                    key="premium", label="Option premium momentum",
                    passed=momentum_ok if liquid and diagnostic.ready else None,
                    detail=(f"{change:+.2f}% since the prior worker observation; need ≥ +0.15%." if change is not None
                            else "Awaiting a second live option observation for momentum confirmation."),
                ))
                option_detail = {
                    "strike": quote.strike, "side": quote.side.value, "ltp": quote.ltp,
                    "bid": quote.bid, "ask": quote.ask, "spread_pct": round(spread_pct, 3),
                    "premium_momentum_pct": round(change, 3) if change is not None else None,
                }
                ready = ready and liquid and momentum_ok
        payload = diagnostic.model_dump(mode="json")
        payload.update({
            "ready": ready,
            "gates": [gate.model_dump(mode="json") for gate in gates],
            "option": option_detail,
            "expiry": expiry,
        })
        if ready:
            payload["summary"] = "Every visible ORB/VWAP gate is currently satisfied; the paper worker will evaluate the entry on its next cycle."
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ORB/VWAP diagnostics failed: {exc}") from exc


def create_dhan_real_trade(request: CreatePaperTradeRequest) -> RealTradeDecision:
    """Submit a protected Dhan Super Order only when real mode is explicitly armed."""
    if execution_mode_store.get().mode is not ExecutionMode.REAL:
        raise HTTPException(status_code=409, detail="Real mode is not armed. Select Real Trade and confirm first.")
    if strategy_profiles.active().paper_trial_only:
        raise HTTPException(
            status_code=409,
            detail="A paper-only strategy trial is active. Select a non-trial profile before arming real orders.",
        )
    prepared: PreparedEntry | None = None
    try:
        prepared = _prepare_option_entry(request, routing_execution_mode=ExecutionMode.REAL)
        receipt = DhanRealOrderClient(get_settings()).place_option_buy(
            security_id=prepared.contract_security_id, quantity=prepared.quantity,
            quote=prepared.quote, request=request,
        )
        return RealTradeDecision(
            accepted=True, reason="Protected Dhan Super Order submitted.", analysis=prepared.analysis,
            order_id=receipt.order_id, order_status=receipt.order_status,
            correlation_id=receipt.correlation_id, order_type=receipt.order_type,
        )
    except EntryRejected as exc:
        return RealTradeDecision(accepted=False, reason=str(exc), analysis=exc.analysis)
    except RealOrderRejected as exc:
        # A gate-approved setup still must disclose a broker refusal clearly.
        assert prepared is not None
        return RealTradeDecision(accepted=False, reason=str(exc), analysis=prepared.analysis)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan real-trade request failed: {exc}") from exc


def create_automated_trade(request: CreatePaperTradeRequest) -> PaperTradeDecision | RealTradeDecision:
    """Route the autonomous worker through the mode selected in the dashboard."""
    # A later dashboard mode change must not turn a forward-paper trial into
    # a Dhan order.  This route remains simulated until the trial is ended.
    if strategy_profiles.active().paper_trial_only:
        return create_dhan_paper_trade(request)
    if execution_mode_store.get().mode is ExecutionMode.REAL:
        return create_dhan_real_trade(request)
    return create_dhan_paper_trade(request)


@app.post("/api/v1/dhan/paper-trades/{trade_id}/mark", response_model=PaperTrade)
def mark_dhan_paper_trade(trade_id: str) -> PaperTrade:
    """Refresh an open paper trade using current Dhan bid prices."""
    settings = get_settings()
    trade = paper_broker.get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Paper trade not found.")
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        snapshot = DhanMarketService(DhanClientFactory(settings)).live_snapshot(
            symbol=trade.symbol,
            security_id=trade.underlying_security_id,
            segment=trade.underlying_segment,
            expiry=trade.expiry,
            instrument_type=trade.underlying_instrument_type,
            vix_security_id=settings.india_vix_security_id,
            vix_segment=settings.india_vix_segment,
            vix_instrument_type=settings.india_vix_instrument_type,
        )
        return paper_broker.mark_to_market(trade_id, snapshot)
    except PaperTradeRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan mark request failed: {exc}") from exc


# ── Autonomous paper trader ────────────────────────────────────────────────


@app.on_event("startup")
def _start_auto_trader() -> None:
    try:
        nse_scrip_master.ensure_current()
        print("[OptionMaster] NSE lot-size master ready.")
    except ScripMasterError as exc:
        # The worker will retry before every orderable entry and expose the
        # final reason in its durable audit; startup itself must stay available.
        print(f"[OptionMaster] NSE lot-size master refresh deferred: {exc}")
    from optionmaster.workers.auto_trader import start_auto_trader
    start_auto_trader(paper_broker)


@app.get("/api/v1/auto/status")
def auto_trader_status() -> dict[str, object]:
    from optionmaster.workers import auto_trader
    return auto_trader.status()


@app.get("/api/v1/auto/attempts", response_model=list[EntryAttempt])
def auto_trader_attempts(limit: int = Query(default=20, ge=1, le=200)) -> list[EntryAttempt]:
    """Latest durable autonomous-entry decisions, including every decline."""
    return journal.list_entry_attempts(limit)


@app.post("/api/v1/auto/toggle")
def auto_trader_toggle(enabled: bool = Query()) -> dict[str, object]:
    from optionmaster.workers import auto_trader
    return auto_trader.set_enabled(enabled)
