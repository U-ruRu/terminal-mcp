"""Durable, bounded scheduling values for managed work-window recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from terminal_mcp.core.work_windows import WorkWindow

MAX_RECOVERY_BATCH = 32
MAX_RECOVERY_LEASE_SECONDS = 30
RECOVERY_ERRORS = frozenset({"execution_pending", "recovery_timeout", "recovery_failed"})


@dataclass(frozen=True, slots=True)
class WindowRecoveryLease:
    token: str
    window: WorkWindow
    attempt: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WindowRecoveryBatch:
    claimed: int = 0
    completed: int = 0
    deferred: int = 0
