"""Runtime execution-mode control."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock

from pydantic import BaseModel


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    REAL = "REAL"


class ExecutionModeChangeRequest(BaseModel):
    mode: ExecutionMode
    confirmation: str = ""


class ExecutionModeState(BaseModel):
    mode: ExecutionMode = ExecutionMode.PAPER
    changed_at: datetime
    real_order_warning: str | None = None


class ExecutionModeStore:
    """In-memory mode switch. A restart always starts in PAPER mode."""

    _real_confirmation = "ENABLE REAL TRADING"

    def __init__(self) -> None:
        self._lock = RLock()
        self._state = ExecutionModeState(mode=ExecutionMode.PAPER, changed_at=datetime.now(timezone.utc))

    def get(self) -> ExecutionModeState:
        with self._lock:
            return self._state.model_copy(deep=True)

    def set(self, request: ExecutionModeChangeRequest) -> ExecutionModeState:
        if request.mode is ExecutionMode.REAL and request.confirmation.strip() != self._real_confirmation:
            raise ValueError("Type ENABLE REAL TRADING to arm real order placement.")
        with self._lock:
            self._state = ExecutionModeState(
                mode=request.mode,
                changed_at=datetime.now(timezone.utc),
                real_order_warning=(
                    "Real mode is armed. Eligible automated entries will send protected Dhan Super Orders."
                    if request.mode is ExecutionMode.REAL else None
                ),
            )
            return self._state.model_copy(deep=True)
