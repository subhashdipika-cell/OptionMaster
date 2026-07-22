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
)
from optionmaster.costs.calculator import NseOptionCostCalculator, NseOptionCostSchedule
from optionmaster.backtest.costs import BacktestOptionTradeRequest, evaluate_backtest_option_trade
from optionmaster.dhan.client import DhanClientFactory
from optionmaster.dhan.service import DhanMarketService
from optionmaster.execution.super_order import SuperOrderIntent, SuperOrderPlanRequest, build_super_order_intent
from optionmaster.instruments.models import QuantityRequest
from optionmaster.instruments.scrip_master import NseScripMaster, ScripMasterError
from optionmaster.instruments.sizing import QuantityResolutionError, resolve_quantity
from optionmaster.learning.service import (
    AutoPromotionResult,
    LearningService,
    PaperPromotionRejected,
    ProfileEvaluation,
    StrategyProfileRegistry,
    UnknownStrategyProfile,
)
from optionmaster.market.features import build_features
from optionmaster.market.models import AnalysisResult, MarketSnapshot, Signal
from optionmaster.journal.store import PerformanceSummary, RecordedBacktestResult, TradeJournal
from optionmaster.paper.broker import PaperBroker, PaperTradeRejected
from optionmaster.paper.models import CreatePaperTradeRequest, PaperTrade, PaperTradeDecision
from optionmaster.risk.gate import apply_risk_gate
from optionmaster.scalping.dhan_stream import DhanScalpingFeed
from optionmaster.scalping.models import MarketTick, ScalpingDecision, ScalpingSession, ScalpingSessionRequest
from optionmaster.scalping.session import ScalpingSessionManager
from optionmaster.strategy.regime import detect_regime
from optionmaster.strategy.profiles import StrategyProfile
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
stored_data_repository = StoredDataRepository(get_settings().stored_data_dir)
spot_candle_repository = SpotCandleRepository(get_settings().spot_data_dir)
nse_scrip_master = NseScripMaster(get_settings().scrip_master_dir)
scalping_sessions = ScalpingSessionManager()
scalping_streams: dict[str, DhanScalpingFeed] = {}


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


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "application": "OptionMaster",
        "version": __version__,
        "execution_mode": settings.execution_mode,
        "dhan_configured": settings.dhan_configured,
        "underlyings": settings.underlying_symbols,
    }


@app.post("/api/v1/analyze", response_model=Signal)
def analyze(snapshot: MarketSnapshot) -> Signal:
    """Analyze a supplied snapshot without placing an order."""
    profile = strategy_profiles.active()
    features = build_features(snapshot)
    regime = detect_regime(features, profile)
    signal = select_signal(snapshot, features, regime, profile)
    return apply_risk_gate(signal, minimum_signal_score=profile.risk_gate_minimum_score)


@app.get("/api/v1/dhan/status")
def dhan_status() -> dict[str, object]:
    settings = get_settings()
    return {
        "configured": settings.dhan_configured,
        "execution_mode": settings.execution_mode,
        "live_orders_enabled": settings.execution_mode == "LIVE",
        "data_access": "available when credentials are configured",
    }


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
        profile = strategy_profiles.active()
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
        features = build_features(snapshot)
        regime = detect_regime(features, profile)
        signal = apply_risk_gate(
            select_signal(snapshot, features, regime, profile),
            minimum_signal_score=profile.risk_gate_minimum_score,
        )
        _record_shadow_context(
            service=service,
            snapshot=snapshot,
            signal=signal,
            security_id=security_id,
            segment=segment,
            instrument_type=instrument_type,
        )
        return AnalysisResult(snapshot=snapshot, features=features, signal=signal)
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
        profile = strategy_profiles.active()
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
        features = build_features(snapshot)
        regime = detect_regime(features, profile)
        signal = apply_risk_gate(
            select_signal(snapshot, features, regime, profile),
            minimum_signal_score=profile.risk_gate_minimum_score,
        )
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
    settings = get_settings()
    if not settings.dhan_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials are not configured.")
    try:
        profile = strategy_profiles.active()
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
        features = build_features(snapshot)
        regime = detect_regime(features, profile)
        signal = select_signal(snapshot, features, regime, profile)
        analysis = AnalysisResult(snapshot=snapshot, features=features, signal=signal)
        context_decision = _record_shadow_context(
            service=service,
            snapshot=snapshot,
            signal=signal,
            security_id=request.security_id,
            segment=request.segment,
            instrument_type=request.instrument_type,
        )
        selected_quote = next(
            (
                quote
                for quote in snapshot.option_quotes
                if quote.side is signal.side and quote.strike == signal.strike
            ),
            None,
        )
        if selected_quote is None or selected_quote.security_id is None:
            return PaperTradeDecision(
                accepted=False,
                reason="The selected contract has no Dhan security ID for lot-size resolution.",
                analysis=analysis,
            )
        try:
            quantity = resolve_quantity(
                scrip_master=nse_scrip_master,
                security_id=selected_quote.security_id,
                lots=request.lots,
                requested_quantity=request.quantity,
            )
            trade = paper_broker.open_from_signal(
                request=request,
                signal=signal,
                snapshot=snapshot,
                quantity=quantity.quantity,
                lot_size=quantity.lot_size,
                contract_security_id=selected_quote.security_id,
                strategy_id=profile.id,
                minimum_signal_score=profile.minimum_signal_score,
                context_decision_id=context_decision.id if context_decision is not None else None,
            )
        except (PaperTradeRejected, QuantityResolutionError) as exc:
            return PaperTradeDecision(accepted=False, reason=str(exc), analysis=analysis)
        return PaperTradeDecision(accepted=True, reason="Paper trade opened.", analysis=analysis, trade=trade)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dhan paper-trade request failed: {exc}") from exc


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
    from optionmaster.workers.auto_trader import start_auto_trader
    start_auto_trader(paper_broker)


@app.get("/api/v1/auto/status")
def auto_trader_status() -> dict[str, object]:
    from optionmaster.workers import auto_trader
    return auto_trader.status()


@app.post("/api/v1/auto/toggle")
def auto_trader_toggle(enabled: bool = Query()) -> dict[str, object]:
    from optionmaster.workers import auto_trader
    return auto_trader.set_enabled(enabled)
