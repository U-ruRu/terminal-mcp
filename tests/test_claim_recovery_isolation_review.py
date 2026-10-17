"""One failed native session cleanup must not starve the next slot."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from terminal_mcp.application.access_mesh import AccessMeshApplication


async def test_one_expired_session_failure_does_not_starve_later_sessions():
    jobs = [
        {
            "job_id": name,
            "logical_agent_id": name,
            "work_session_id": name,
            "session_epoch": 1,
            "reason": "expired",
            "release_claims": True,
        }
        for name in ("bad", "good")
    ]
    attempts, finished = [], []

    async def fence(agent_id, *args, **kwargs):
        attempts.append(agent_id)
        if agent_id == "bad":
            raise RuntimeError("one session cleanup failed")
        return []

    mesh = object.__new__(AccessMeshApplication)
    mesh._cleanup_lock = asyncio.Lock()
    mesh.store = SimpleNamespace(
        pending_cleanup=lambda **kwargs: jobs,
        finish_cleanup=lambda job_id, **kwargs: finished.append((job_id, kwargs["error"])),
    )
    mesh.execution_fence = SimpleNamespace(revoke_session=fence)
    mesh.task_store = SimpleNamespace(
        release_owner_claims_mutation=AsyncMock(return_value={"ok": True})
    )
    await mesh.cleanup()
    assert attempts == ["bad", "good"]
    assert finished == [("bad", "RuntimeError"), ("good", None)]
