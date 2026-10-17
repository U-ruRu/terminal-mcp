"""Local execution serialization shared by every native Access session."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class SessionExecutionError(RuntimeError):
    def __init__(self, code: str, *, blockers: list[dict] | None = None):
        self.code = code
        self.blockers = blockers or []
        super().__init__(code)


class SessionExecutionCoordinator:
    """Serialize one slot's writes without owning a second session lifecycle."""

    def __init__(self, store, *, session_duration_seconds: int, rearm_delay_seconds: int):
        self.store = store
        self.session_duration_seconds = session_duration_seconds
        self.rearm_delay_seconds = rearm_delay_seconds
        self._operation_locks = WeakValueDictionary()
        self._policy_lock = asyncio.Lock()

    def _lock(self, logical_agent_id: str) -> asyncio.Lock:
        lock = self._operation_locks.get(logical_agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[logical_agent_id] = lock
        return lock

    @asynccontextmanager
    async def operation_guard(self, logical_agent_id: str):
        async with self._lock(logical_agent_id):
            yield

    @asynccontextmanager
    async def policy_guard(self):
        async with self._policy_lock:
            yield
