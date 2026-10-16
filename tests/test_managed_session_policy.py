"""Managed-session operation policy owns lifecycle admission independently of application wiring."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.managed_sessions import (
    FINALIZATION_OPERATIONS,
    OPERATOR_OPERATIONS,
    READ_OPERATIONS,
    ManagedOperation,
    ManagedSessionError,
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
        ManagedOperation.SESSION_STATUS,
        ManagedOperation.OBSERVE,
        ManagedOperation.TASK_DONE,
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


def test_operation_classification_is_owned_by_exact_sets():
    assert READ_OPERATIONS == frozenset(
        {
            ManagedOperation.SESSION_STATUS,
            ManagedOperation.OBSERVE,
            ManagedOperation.HEALTH,
            ManagedOperation.COMMAND_READ,
            ManagedOperation.TASK_LIST,
            ManagedOperation.MESSAGE_READ,
            ManagedOperation.CONTEXT_READ,
            ManagedOperation.OPERATOR_READ,
        }
    )
    assert FINALIZATION_OPERATIONS == READ_OPERATIONS | frozenset(
        {
            ManagedOperation.SESSION_END,
            ManagedOperation.COMMAND_CANCEL,
            ManagedOperation.TASK_CHECKPOINT,
            ManagedOperation.TASK_DONE,
            ManagedOperation.TASK_RELEASE,
            ManagedOperation.MESSAGE_ACK,
            ManagedOperation.MESSAGE_REPLY,
        }
    )
    assert OPERATOR_OPERATIONS == frozenset(
        {
            ManagedOperation.OPERATOR_READ,
            ManagedOperation.OPERATOR_POLICY,
            ManagedOperation.OPERATOR_WINDOW,
            ManagedOperation.OPERATOR_END,
        }
    )
    assert frozenset(ManagedOperation) == FINALIZATION_OPERATIONS | frozenset(
        {
            ManagedOperation.SESSION_START,
            ManagedOperation.COMMAND_RUN,
            ManagedOperation.COMMAND_RECOVERY,
            ManagedOperation.TASK_CREATE,
            ManagedOperation.TASK_CLAIM,
            ManagedOperation.TASK_UPDATE,
            ManagedOperation.TASK_REVIEW,
            ManagedOperation.TASK_COMMENT,
            ManagedOperation.TASK_RELATE,
            ManagedOperation.TASK_UNRELATE,
            ManagedOperation.TASK_STATE,
            ManagedOperation.TASK_ARCHIVE,
            ManagedOperation.MESSAGE_SEND,
            ManagedOperation.CONTEXT_WRITE,
            ManagedOperation.OPERATOR_POLICY,
            ManagedOperation.OPERATOR_WINDOW,
            ManagedOperation.OPERATOR_END,
        }
    )


@pytest.mark.parametrize("operation", ["task.done", ManagedOperation.OPERATOR_WINDOW])
def test_unknown_and_operator_operations_do_not_bypass_agent_lifecycle(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    with pytest.raises(ManagedSessionError, match="operation_not_allowed"):
        decide_session_operation(window, operation, T0)
