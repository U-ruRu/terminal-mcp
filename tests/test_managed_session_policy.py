"""Managed-session operation policy owns lifecycle admission independently of application wiring."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.managed_sessions import (
    FINALIZATION_OPERATIONS,
    OPERATOR_OPERATIONS,
    ManagedOperation,
    ManagedSessionError,
    decide_session_operation,
)
from terminal_mcp.core.work_windows import SlotSessionPolicy, WindowPhase, WorkWindow

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "operation", [op for op in ManagedOperation if op not in OPERATOR_OPERATIONS]
)
def test_pure_policy_exhaustively_classifies_draining(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    decision = decide_session_operation(window, operation, T0 + timedelta(seconds=1320))
    assert decision.phase is WindowPhase.DRAINING
    assert decision.remaining_seconds == 60
    assert decision.allowed == (operation in FINALIZATION_OPERATIONS)
    assert decision.return_to_chat
    assert decision.code == (None if decision.allowed else "session_draining")


@pytest.mark.parametrize(
    "operation", [op for op in ManagedOperation if op not in OPERATOR_OPERATIONS]
)
def test_pure_policy_expiry_never_admits_new_work(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    decision = decide_session_operation(window, operation, T0 + timedelta(seconds=1380))
    assert decision.remaining_seconds == 0
    assert decision.return_to_chat
    assert decision.allowed == (
        operation in {ManagedOperation.SESSION_END, ManagedOperation.SESSION_STATUS}
    )


@pytest.mark.parametrize("operation", ["new.tool", "task.done", ManagedOperation.OPERATOR_WINDOW])
def test_unknown_and_operator_operations_do_not_bypass_agent_lifecycle(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    with pytest.raises(ManagedSessionError, match="operation_not_allowed"):
        decide_session_operation(window, operation, T0)
