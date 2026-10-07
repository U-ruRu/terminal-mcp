"""Host-owned lifecycle for managed-session recovery.

The runtime owns exactly one bounded reconciliation task. It is composed and
started explicitly by the host after durable stores and execution/fleet ports are
ready; importing managed-session modules never starts background work.
"""

from __future__ import annotations

import asyncio
import logging
from math import isfinite

from terminal_mcp.application.managed_identity import ManagedProviderResolver
from terminal_mcp.application.managed_sessions import ManagedSessionApplication
from terminal_mcp.application.window_recovery import ManagedWindowRecovery

_LOG = logging.getLogger(__name__)


class ManagedSessionRuntime:
    def __init__(
        self,
        identity: ManagedProviderResolver,
        sessions: ManagedSessionApplication,
        recovery: ManagedWindowRecovery,
        *,
        enabled: bool,
    ):
        self.identity = identity
        self.sessions = sessions
        self.recovery = recovery
        self.enabled = bool(enabled)
        self._recovery_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def running(self) -> bool:
        task = self._recovery_task
        return task is not None and not task.done()

    async def start(self, interval_seconds: float = 1.0) -> None:
        if not self.enabled or self.running:
            return
        if (
            isinstance(interval_seconds, bool)
            or not isfinite(interval_seconds)
            or interval_seconds < 0.2
        ):
            raise ValueError("managed recovery interval must be finite and at least 0.2 seconds")
        self._stopped.clear()
        # Reconcile once before managed capabilities can be used. The tick itself
        # is bounded by durable lease, batch, concurrency and per-item limits. A
        # transient repository failure must not make the whole API fail startup.
        try:
            await self.recovery.tick()
        except Exception as exc:
            _LOG.warning("initial managed window recovery tick failed: %s", type(exc).__name__)
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(float(interval_seconds)),
            name="managed-window-reconciler",
        )

    async def stop(self) -> None:
        self._stopped.set()
        task = self._recovery_task
        self._recovery_task = None
        if task is not None:
            # Wake a sleeping loop cooperatively. Do not cancel an in-flight
            # SQLite recovery transaction and leave its worker thread targeting
            # an event loop that is already closing. Each tick is independently
            # bounded, so graceful completion has a finite upper bound.
            await asyncio.gather(task, return_exceptions=True)

    async def _recovery_loop(self, interval_seconds: float) -> None:
        while not self._stopped.is_set():
            try:
                async with asyncio.timeout(interval_seconds):
                    await self._stopped.wait()
                break
            except TimeoutError:
                pass
            try:
                await self.recovery.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Recovery is durable and leased. One failed pass must not kill
                # the host or turn into a tight retry loop.
                _LOG.warning("managed window recovery tick failed: %s", type(exc).__name__)
