"""Managed-session operation policy owns lifecycle admission independently of application wiring."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.managed_sessions import (
    FINALIZATION_OPERATIONS,
    ManagedOperation,
    decide_session_operation,
)
from terminal_mcp.core.work_windows import SlotSessionPolicy, WindowPhase, WorkWindow

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "operation",
    [ManagedOperation.OBSERVE, ManagedOperation.TASK_DONE, ManagedOperation.TASK_CREATE],
)
def test_draining_read_finalization_and_new_work_branches(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    decision = decide_session_operation(window, operation, T0 + timedelta(seconds=1320))
    assert decision.phase is WindowPhase.DRAINING
    assert decision.remaining_seconds == 60
    assert decision.allowed == (operation in FINALIZATION_OPERATIONS)
    assert decision.return_to_chat
    assert decision.code == (None if decision.allowed else "session_draining")


@pytest.mark.parametrize(
    "operation",
    [
        ManagedOperation.SESSION_END,
        ManagedOperation.OBSERVE,
    ],
)
def test_expiry_admits_only_end_and_status(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    decision = decide_session_operation(window, operation, T0 + timedelta(seconds=1380))
    assert decision.phase is WindowPhase.EXPIRED
    assert decision.remaining_seconds == 0
    assert decision.return_to_chat
    assert decision.allowed == (
        operation in {ManagedOperation.SESSION_END, ManagedOperation.SESSION_STATUS}
    )


def test_cooldown_does_not_admit_new_work():
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    window = window.expire(now=T0 + timedelta(seconds=1380), expected_revision=1)
    window = window.begin_cooldown(expected_revision=2)
    decision = decide_session_operation(
        window, ManagedOperation.COMMAND_RUN, T0 + timedelta(seconds=1500)
    )
    assert decision.phase is WindowPhase.COOLDOWN
    assert decision.allowed is False
    assert decision.code == "session_expired"
