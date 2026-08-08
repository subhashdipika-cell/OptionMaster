import pytest

from optionmaster.execution.mode import (
    ExecutionMode,
    ExecutionModeChangeRequest,
    ExecutionModeStore,
)


def test_real_mode_requires_deliberate_confirmation():
    store = ExecutionModeStore()

    with pytest.raises(ValueError, match="ENABLE REAL TRADING"):
        store.set(ExecutionModeChangeRequest(mode=ExecutionMode.REAL))

    state = store.set(
        ExecutionModeChangeRequest(mode=ExecutionMode.REAL, confirmation="ENABLE REAL TRADING")
    )
    assert state.mode is ExecutionMode.REAL

    assert store.set(ExecutionModeChangeRequest(mode=ExecutionMode.PAPER)).mode is ExecutionMode.PAPER
