import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel

from optionmaster.context.models import (
    ContextDecision,
    ContextOutcomeReport,
    ContextShadowSummary,
    FeatureOutcomeBucket,
    ShadowAction,
)
from optionmaster.costs.calculator import NetTradeResult
from optionmaster.paper.models import PaperTrade
from optionmaster.scalping.models import ScalpingDecision


class PerformanceSummary(BaseModel):
    scope: str
    strategy_id: str | None = None
    closed_trades: int
    gross_pnl: float
    charges: float
    net_pnl: float
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float | None = None


class RecordedBacktestResult(BaseModel):
    id: str
    recorded_at: datetime
    strategy_id: str
    result: NetTradeResult


class TradeJournal:
    """SQLite journal that preserves cost-aware evidence across restarts."""

    def __init__(self, path: str | Path = "data/optionmaster.db") -> None:
        self._path = Path(path)
        self._lock = RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def record_paper_trade(self, trade: PaperTrade) -> None:
        payload = trade.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (id, strategy_id, context_decision_id, status, gross_pnl, net_pnl, charges, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    strategy_id=excluded.strategy_id,
                    context_decision_id=excluded.context_decision_id,
                    status=excluded.status,
                    gross_pnl=excluded.gross_pnl,
                    net_pnl=excluded.net_pnl,
                    charges=excluded.charges,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    trade.id,
                    trade.strategy_id,
                    trade.context_decision_id,
                    trade.status.value,
                    trade.gross_pnl,
                    trade.realized_pnl if trade.realized_pnl is not None else trade.unrealized_pnl,
                    trade.charges.total if trade.charges else 0.0,
                    payload,
                    now,
                ),
            )

    def list_paper_trades(self) -> list[PaperTrade]:
        with self._connection() as conn:
            rows = conn.execute("SELECT payload FROM paper_trades ORDER BY updated_at DESC").fetchall()
        return [PaperTrade.model_validate_json(row[0]) for row in rows]

    def record_backtest_trade(self, result: NetTradeResult, *, strategy_id: str) -> RecordedBacktestResult:
        record_id = uuid4().hex
        recorded_at = datetime.now(timezone.utc)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO backtest_trades (id, strategy_id, gross_pnl, net_pnl, charges, payload, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    strategy_id,
                    result.gross_pnl,
                    result.net_pnl,
                    result.costs.total,
                    result.model_dump_json(),
                    recorded_at.isoformat(),
                ),
            )
        return RecordedBacktestResult(
            id=record_id,
            recorded_at=recorded_at,
            strategy_id=strategy_id,
            result=result,
        )

    def record_backtest_run(self, run) -> None:
        """Persist a full historical backtest run, including its trade list."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO backtest_runs (id, strategy_id, net_pnl, trades, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.strategy_id,
                    run.summary.net_pnl,
                    run.summary.trades,
                    run.model_dump_json(),
                    run.created_at.isoformat(),
                ),
            )

    def list_backtest_runs(self, limit: int = 50) -> list[str]:
        """Return stored run payloads, newest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_backtest_run(self, run_id: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM backtest_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def record_scalping_decision(self, session_id: str, decision: ScalpingDecision) -> None:
        """Persist only actionable decisions to avoid writing every raw feed tick."""
        if decision.action.value == "SKIP":
            return
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO scalping_decisions (id, session_id, action, payload, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    session_id,
                    decision.action.value,
                    decision.model_dump_json(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_context_decision(self, decision: ContextDecision) -> None:
        """Persist each shadow-context evaluation, including filters that would skip a setup."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO context_decisions (id, symbol, strategy_id, action, confluence_score, payload, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.symbol,
                    decision.strategy_id,
                    decision.action.value,
                    decision.confluence.total,
                    decision.model_dump_json(),
                    decision.timestamp.isoformat(),
                ),
            )

    def context_shadow_summary(self) -> ContextShadowSummary:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT action, confluence_score, recorded_at FROM context_decisions"
            ).fetchall()
        actions = [str(row[0]) for row in rows]
        directional = sum(action != ShadowAction.NO_DIRECTIONAL_SIGNAL.value for action in actions)
        scores = [float(row[1]) for row in rows]
        latest = max((datetime.fromisoformat(str(row[2])) for row in rows), default=None)
        return ContextShadowSummary(
            total_evaluations=len(rows),
            directional_signals=directional,
            would_allow=actions.count(ShadowAction.WOULD_ALLOW.value),
            would_skip=actions.count(ShadowAction.WOULD_SKIP.value),
            average_confluence_score=round(sum(scores) / len(scores), 2) if scores else 0.0,
            latest_evaluated_at=latest,
        )

    def context_outcome_report(self) -> ContextOutcomeReport:
        """Compare closed paper results against the shadow features captured at signal time."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT paper_trades.net_pnl, context_decisions.payload
                FROM paper_trades
                JOIN context_decisions ON paper_trades.context_decision_id = context_decisions.id
                WHERE paper_trades.status != ?
                """,
                ("OPEN",),
            ).fetchall()
        groups: dict[str, dict[str, list[float]]] = {
            "freshness": {"Fresh": [], "Extended / stale": []},
            "confluence": {"Below 70": [], "70–84": [], "85+": []},
            "structure_room": {"At least 1.5R": [], "Less than 1.5R / unavailable": []},
        }
        for net_pnl, payload in rows:
            decision = ContextDecision.model_validate_json(str(payload))
            value = float(net_pnl)
            groups["freshness"]["Fresh" if decision.freshness.fresh else "Extended / stale"].append(value)
            score = decision.confluence.total
            confluence_label = "85+" if score >= 85 else "70–84" if score >= 70 else "Below 70"
            groups["confluence"][confluence_label].append(value)
            has_room = decision.risk_reward_to_structure is not None and decision.risk_reward_to_structure >= 1.5
            groups["structure_room"]["At least 1.5R" if has_room else "Less than 1.5R / unavailable"].append(value)
        return ContextOutcomeReport(
            linked_closed_trades=len(rows),
            ready_for_feature_review=len(rows) >= 200,
            freshness=self._feature_buckets(groups["freshness"]),
            confluence=self._feature_buckets(groups["confluence"]),
            structure_room=self._feature_buckets(groups["structure_room"]),
        )

    @staticmethod
    def _feature_buckets(groups: dict[str, list[float]]) -> list[FeatureOutcomeBucket]:
        buckets: list[FeatureOutcomeBucket] = []
        for label, values in groups.items():
            wins = sum(value > 0 for value in values)
            losses = sum(value < 0 for value in values)
            buckets.append(
                FeatureOutcomeBucket(
                    label=label,
                    closed_trades=len(values),
                    wins=wins,
                    losses=losses,
                    win_rate_pct=round((wins / len(values)) * 100, 2) if values else 0.0,
                    net_pnl=round(sum(values), 2),
                )
            )
        return buckets

    def performance(self, scope: str, *, strategy_id: str | None = None) -> PerformanceSummary:
        if scope not in {"paper", "backtest"}:
            raise ValueError("Performance scope must be 'paper' or 'backtest'.")
        table = "paper_trades" if scope == "paper" else "backtest_trades"
        filters: list[str] = []
        values: list[str] = []
        if scope == "paper":
            filters.append("status != ?")
            values.append("OPEN")
        if strategy_id is not None:
            filters.append("strategy_id = ?")
            values.append(strategy_id)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT gross_pnl, net_pnl, charges FROM {table}{where}",  # table is a fixed constant
                values,
            ).fetchall()
        gross = round(sum(float(row[0]) for row in rows), 2)
        net = round(sum(float(row[1]) for row in rows), 2)
        charges = round(sum(float(row[2]) for row in rows), 2)
        wins = sum(1 for row in rows if float(row[1]) > 0)
        losses = sum(1 for row in rows if float(row[1]) < 0)
        profit = sum(float(row[1]) for row in rows if float(row[1]) > 0)
        loss = abs(sum(float(row[1]) for row in rows if float(row[1]) < 0))
        return PerformanceSummary(
            scope=scope,
            strategy_id=strategy_id,
            closed_trades=len(rows),
            gross_pnl=gross,
            charges=charges,
            net_pnl=net,
            wins=wins,
            losses=losses,
            win_rate_pct=round((wins / len(rows)) * 100, 2) if rows else 0.0,
            profit_factor=round(profit / loss, 4) if loss else None,
        )

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL DEFAULT 'baseline-v1',
                    context_decision_id TEXT,
                    status TEXT NOT NULL,
                    gross_pnl REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    charges REAL NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_trades (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL DEFAULT 'baseline-v1',
                    gross_pnl REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    charges REAL NOT NULL,
                    payload TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scalping_decisions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_decisions (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confluence_score REAL NOT NULL,
                    payload TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    net_pnl REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "paper_trades", "strategy_id", "TEXT NOT NULL DEFAULT 'baseline-v1'")
            self._ensure_column(conn, "paper_trades", "context_decision_id", "TEXT")
            self._ensure_column(conn, "backtest_trades", "strategy_id", "TEXT NOT NULL DEFAULT 'baseline-v1'")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_active_strategy_id(self, *, default: str) -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM strategy_state WHERE key = ?", ("active_strategy_id",)
            ).fetchone()
            if row is not None:
                return str(row[0])
            conn.execute(
                "INSERT INTO strategy_state (key, value, updated_at) VALUES (?, ?, ?)",
                ("active_strategy_id", default, datetime.now(timezone.utc).isoformat()),
            )
        return default

    def set_active_strategy_id(self, strategy_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO strategy_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                ("active_strategy_id", strategy_id, datetime.now(timezone.utc).isoformat()),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Provide a short-lived, committed SQLite connection for every journal action."""
        with self._lock:
            conn = sqlite3.connect(self._path, check_same_thread=False, timeout=5)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
