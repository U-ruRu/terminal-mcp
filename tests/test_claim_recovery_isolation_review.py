"""Independent regression: one invalid lease must not starve other expired claims."""

from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_fleet import PersistentFleetBridge


class Store:
    async def expired_remote_permit_sessions(self):
        return [
            {"logical_agent_id": "la_bad", "work_session_id": "ws_bad", "session_epoch": 1},
            {"logical_agent_id": "la_good", "work_session_id": "ws_good", "session_epoch": 2},
        ]


class Tasks:
    async def expired_claim_sessions(self, **kwargs):
        return []


class FakeBridge:
    execution_fence = object()
    config = SimpleNamespace(instance_id="firstbyte")
    store = Store()
    task_store = Tasks()

    def __init__(self):
        self.attempts = []

    async def receive_revoke(self, **data):
        self.attempts.append(data["logical_agent_id"])
        if data["logical_agent_id"] == "la_bad":
            raise RuntimeError("single expired lease cleanup failed")
        return []


@pytest.mark.asyncio
async def test_one_expired_session_failure_does_not_starve_later_sessions():
    bridge = FakeBridge()
    try:
        await PersistentFleetBridge.reconcile_remote_expiry(bridge)
    except RuntimeError:
        pass
    assert "la_good" in bridge.attempts, "subsequent expired lease was starved by earlier failure"
