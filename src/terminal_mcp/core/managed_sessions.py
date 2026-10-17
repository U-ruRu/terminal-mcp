"""Native execution operation categories and lifecycle errors."""

from __future__ import annotations

from enum import StrEnum


class ManagedSessionError(RuntimeError):
    def __init__(self, code: str, *, current=None, return_to_chat: bool = False):
        self.code = code
        self.current = current
        self.return_to_chat = return_to_chat
        super().__init__(code)


class ManagedOperation(StrEnum):
    SESSION_STATUS = "session.status"
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
        ManagedOperation.COMMAND_CANCEL,
        ManagedOperation.TASK_CHECKPOINT,
        ManagedOperation.TASK_DONE,
        ManagedOperation.TASK_RELEASE,
        ManagedOperation.MESSAGE_ACK,
        ManagedOperation.MESSAGE_REPLY,
    }
)
