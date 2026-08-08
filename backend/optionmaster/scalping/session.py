from threading import RLock
from uuid import uuid4

from optionmaster.scalping.breakout_retest import BreakoutRetestScalpingEngine
from optionmaster.scalping.engine import ScalpingEngine
from optionmaster.scalping.models import (
    MarketTick, ScalpingDecision, ScalpingSession, ScalpingSessionRequest, ScalpingStrategy,
)


class ScalpingSessionManager:
    def __init__(self) -> None:
        self._engines: dict[str, ScalpingEngine] = {}
        self._sessions: dict[str, ScalpingSession] = {}
        self._lock = RLock()

    def create(self, configuration: ScalpingSessionRequest) -> ScalpingSession:
        session_id = uuid4().hex
        session = ScalpingSession(id=session_id, configuration=configuration)
        with self._lock:
            self._engines[session_id] = (
                BreakoutRetestScalpingEngine(configuration)
                if configuration.strategy in (
                    ScalpingStrategy.BREAKOUT_RETEST,
                    ScalpingStrategy.BREAKOUT_RETEST_3_BAR,
                )
                else ScalpingEngine(configuration)
            )
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> ScalpingSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def ingest(self, session_id: str, tick: MarketTick) -> ScalpingDecision:
        with self._lock:
            engine = self._engines.get(session_id)
            session = self._sessions.get(session_id)
            if engine is None or session is None:
                raise KeyError(session_id)
            decision = engine.ingest(tick)
            session.latest_decision = decision
            return decision

    def set_running(self, session_id: str, running: bool, error: str | None = None) -> ScalpingSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.running = running
            session.last_error = error
            return session
