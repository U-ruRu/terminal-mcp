"""Host reconciliation lifecycle with neighboring recovery/identity/session ports doubled."""

import asyncio

import pytest

from terminal_mcp.application.managed_runtime import ManagedSessionRuntime


class _Recovery:
    def __init__(self, *, failures=0):
        self.calls = 0
        self.failures = failures
        self.completed = asyncio.Event()

    async def tick(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient")
        self.completed.set()


class _Port:
    pass


@pytest.mark.asyncio
async def test_initial_tick_failure_does_not_stop_periodic_reconciliation():
    recovery = _Recovery(failures=1)
    runtime = ManagedSessionRuntime(_Port(), _Port(), recovery, enabled=True)

    try:
        await runtime.start(interval_seconds=0.2)
        await asyncio.wait_for(recovery.completed.wait(), timeout=1)
        assert runtime.running
    finally:
        await runtime.stop()
