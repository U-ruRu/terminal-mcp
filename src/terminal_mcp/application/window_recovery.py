"""Explicit bounded recovery ticks; no autonomous tasks are started at import."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from math import isfinite
from typing import Protocol

from terminal_mcp.application.managed_sessions import ManagedSessionApplication
from terminal_mcp.core.orchestration import utc_now
from terminal_mcp.core.window_recovery import (
    MAX_RECOVERY_BATCH,
    WindowRecoveryBatch,
    WindowRecoveryLease,
)


class WindowRecoveryRepository(Protocol):
    async def claim_window_recovery(
        self, *, limit: int = 8, lease_seconds: int = 30, now: datetime | None = None
    ) -> list[WindowRecoveryLease]: ...
    async def finish_window_recovery(
        self,
        lease: WindowRecoveryLease,
        *,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> bool: ...


class ManagedWindowRecovery:
    def __init__(
        self,
        repository: WindowRecoveryRepository,
        application: ManagedSessionApplication,
        *,
        batch_size: int = 8,
        concurrency: int = 4,
        item_timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] = utc_now,
    ):
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_RECOVERY_BATCH:
            raise ValueError("recovery batch size must be between 1 and 32")
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise ValueError("recovery concurrency must be between 1 and 4")
        if (
            isinstance(item_timeout_seconds, bool)
            or not isfinite(item_timeout_seconds)
            or not 0 < item_timeout_seconds <= 5
        ):
            raise ValueError("recovery item timeout must be positive and at most five seconds")
        # A full worst-case batch must fit inside its durable lease; otherwise
        # another worker could reclaim items that are still waiting locally.
        if ((batch_size + concurrency - 1) // concurrency) * (item_timeout_seconds + 1) >= 25:
            raise ValueError("recovery batch must fit its durable lease budget")
        self.repository = repository
        self.application = application
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.item_timeout_seconds = item_timeout_seconds
        self.clock = clock
        self._tick_guard = asyncio.Lock()

    async def tick(self) -> WindowRecoveryBatch:
        # Serialize this instance; multiple processes still coordinate through
        # transactional expiring leases. No task count grows with total slots.
        async with self._tick_guard:
            leases = await self.repository.claim_window_recovery(
                limit=self.batch_size,
                lease_seconds=30,
                now=self.clock(),
            )
            semaphore = asyncio.Semaphore(self.concurrency)

            async def recover(lease):
                async with semaphore:
                    error = None
                    complete = False
                    try:
                        async with asyncio.timeout(self.item_timeout_seconds):
                            complete = await self.application.reconcile_window(lease.window)
                            if not complete:
                                error = "execution_pending"
                    except TimeoutError:
                        error = "recovery_timeout"
                    except Exception:
                        error = "recovery_failed"
                    # Cancellation is not swallowed; abandoned leases naturally
                    # expire, and revoked execution authority never comes back.
                    try:
                        async with asyncio.timeout(1):
                            await self.repository.finish_window_recovery(
                                lease,
                                error_code=error,
                                now=self.clock(),
                            )
                    except Exception:
                        complete = False
                    return complete

            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(recover(lease)) for lease in leases]
            completed = sum(task.result() for task in tasks)
            return WindowRecoveryBatch(len(leases), completed, len(leases) - completed)
