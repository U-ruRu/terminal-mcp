"""Transport-independent managed-session values and fail-closed lifecycle policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from terminal_mcp.core.persistent_agents import WorkSessionRecord
from terminal_mcp.core.work_windows import (
    SlotSessionPolicy,
    WindowPhase,
    WorkSessionBinding,
    WorkWindow,
)


class ManagedSessionError(RuntimeError):
    def __init__(self, code: str, *, current=None, return_to_chat: bool = False):
        self.code = code
        self.current = current
        self.return_to_chat = return_to_chat
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SlotPolicyRecord:
    policy: SlotSessionPolicy
    revision: int


@dataclass(frozen=True, slots=True)
class ManagedSessionSnapshot:
    window: WorkWindow
    session: WorkSessionRecord
    binding: WorkSessionBinding
    created: bool = False


class ManagedOperation(StrEnum):
    SESSION_START = "session.start"
    SESSION_STATUS = "session.status"
    SESSION_END = "session.end"
    OBSERVE = "observe"
    HEALTH = "health"
    COMMAND_RUN = "command.run"
    COMMAND_READ = "command.read"
    COMMAND_CANCEL = "command.cancel"
    COMMAND_RECOVERY = "command.recovery"
    TASK_LIST = "task.list"
    TASK_CREATE = "task.create"
    TASK_CLAIM = "task.claim"
    TASK_UPDATE = "task.update"
    TASK_CHECKPOINT = "task.checkpoint"
    TASK_DONE = "task.done"
    TASK_RELEASE = "task.release"
    TASK_REVIEW = "task.review"
    TASK_COMMENT = "task.comment"
    TASK_RELATE = "task.relate"
    TASK_UNRELATE = "task.unrelate"
    TASK_STATE = "task.state"
    TASK_ARCHIVE = "task.archive"
    MESSAGE_READ = "message.read"
    MESSAGE_SEND = "message.send"
    MESSAGE_ACK = "message.ack"
    MESSAGE_REPLY = "message.reply"
    CONTEXT_READ = "context.read"
    CONTEXT_WRITE = "context.write"
    OPERATOR_READ = "operator.read"
    OPERATOR_POLICY = "operator.policy"
    OPERATOR_WINDOW = "operator.window"
    OPERATOR_END = "operator.end"


READ_OPERATIONS = frozenset(
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
FINALIZATION_OPERATIONS = READ_OPERATIONS | frozenset(
    {
        ManagedOperation.SESSION_START,
        ManagedOperation.SESSION_END,
        ManagedOperation.COMMAND_CANCEL,
        ManagedOperation.TASK_CHECKPOINT,
        ManagedOperation.TASK_DONE,
        ManagedOperation.TASK_RELEASE,
        ManagedOperation.MESSAGE_ACK,
        ManagedOperation.MESSAGE_REPLY,
    }
)
OPERATOR_OPERATIONS = frozenset(
    {
        ManagedOperation.OPERATOR_READ,
        ManagedOperation.OPERATOR_POLICY,
        ManagedOperation.OPERATOR_WINDOW,
        ManagedOperation.OPERATOR_END,
    }
)


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    allowed: bool
    phase: WindowPhase
    remaining_seconds: int
    return_to_chat: bool
    code: str | None = None


def decide_session_operation(
    window: WorkWindow, operation: ManagedOperation, now: datetime
) -> LifecycleDecision:
    if not isinstance(operation, ManagedOperation) or operation in OPERATOR_OPERATIONS:
        raise ManagedSessionError("operation_not_allowed")
    phase = window.phase(now)
    remaining = window.remaining_seconds(now)
    if phase in {WindowPhase.EXPIRED, WindowPhase.COOLDOWN}:
        allowed = operation in {ManagedOperation.SESSION_END, ManagedOperation.SESSION_STATUS}
        return LifecycleDecision(allowed, phase, 0, True, None if allowed else "session_expired")
    if phase is WindowPhase.DRAINING and operation not in FINALIZATION_OPERATIONS:
        return LifecycleDecision(False, phase, remaining, True, "session_draining")
    return LifecycleDecision(True, phase, remaining, phase is WindowPhase.DRAINING)
