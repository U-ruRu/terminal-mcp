"""Persistent backend task-scope admission with lifecycle and execution-authority ports doubled."""

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend


class _RemoteOnlyLifecycle:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def operation_guard(self, logical_agent_id):
        yield


class _RemotePermitBackend(PersistentBackend):
    def __init__(self):
        self.lifecycle = _RemoteOnlyLifecycle()

    async def _execution_authority(self, *args, **kwargs):
        return None, object()


@pytest.mark.asyncio
async def test_remote_persistent_run_rejects_task_scope_without_distributed_claim_fence():
    backend = _RemotePermitBackend()
    result = await backend.run(
        "true",
        logical_agent_id="logical-remote",
        work_session_id="ws-remote",
        session_epoch=7,
        queue_id=1,
        task_scope="all",
    )
    assert result == {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
