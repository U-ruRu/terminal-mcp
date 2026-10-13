"""Host reconciliation lifecycle with neighboring recovery/identity/session ports doubled."""

import asyncio

import pytest

from terminal_mcp.application.managed_runtime import ManagedSessionRuntime


class _Recovery:
    def __init__(self, *, failures=0):
        self.calls = 0
        self.failures = failures

    async def tick(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient")


class _Port:
    pass


@pytest.mark.asyncio
async def test_initial_tick_failure_does_not_stop_periodic_reconciliation():
    recovery = _Recovery(failures=1)
    runtime = ManagedSessionRuntime(_Port(), _Port(), recovery, enabled=True)

    try:
        await runtime.start(interval_seconds=0.2)
        assert runtime.running
        # Initial failure is contained; the next durable pass still runs.
        await asyncio.sleep(0.24)
        assert recovery.calls >= 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stop_finishes_the_host_task_and_prevents_later_recovery_ticks():
    recovery = _Recovery()
    runtime = ManagedSessionRuntime(_Port(), _Port(), recovery, enabled=True)
    await runtime.start(interval_seconds=0.2)
    await runtime.stop()
    assert not runtime.running

    calls = recovery.calls
    await asyncio.sleep(0.22)
    assert recovery.calls == calls


@pytest.mark.asyncio
async def test_managed_runtime_disabled_never_starts_recovery():
    recovery = _Recovery()
    runtime = ManagedSessionRuntime(_Port(), _Port(), recovery, enabled=False)
    await runtime.start()
    assert not runtime.running
    assert recovery.calls == 0
