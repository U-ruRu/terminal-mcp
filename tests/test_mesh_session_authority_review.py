"""Tango-15: independent negative/positive mesh authority checks."""

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError


class Bridge:
    def __init__(self, actual_home):
        self.actual_home = actual_home
        self.calls = []

    async def route_info(self, logical_agent_id):
        return {"authority_node_id": self.actual_home, "authority_epoch": 4, "state": "active"}

    async def receive_revoke(self, **kwargs):
        self.calls.append(("revoke", kwargs))
        return []

    async def receive_drain(self, **kwargs):
        self.calls.append(("drain", kwargs))
        return []


@pytest.mark.asyncio
async def test_authenticated_non_authority_cannot_revoke_foreign_session():
    bridge = Bridge(actual_home="bacloud")
    app = MeshApplication(bridge)
    actor = ActorContext(endpoint_role="mesh", peer_node_id="firstbyte", node_id="secondary")
    payload = {
        "authority_node_id": "firstbyte",
        "logical_agent_id": "la_foreign",
        "work_session_id": "ws_foreign",
        "session_epoch": 1,
        "hard_expires_at": "2026-10-08T02:00:00Z",
        "reason": "review",
    }
    with pytest.raises(MeshApplicationError):
        await app.revoke(actor, payload)
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_session_control_rejects_stale_authority_epoch():
    bridge = Bridge(actual_home="bacloud")
    app = MeshApplication(bridge)
    actor = ActorContext(endpoint_role="mesh", peer_node_id="bacloud")
    with pytest.raises(MeshApplicationError):
        await app.revoke(
            actor,
            dict(
                authority_node_id="bacloud",
                authority_epoch=3,
                logical_agent_id="la_home",
                work_session_id="ws_home",
                session_epoch=1,
                hard_expires_at="2026-10-08T02:00:00Z",
            ),
        )
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_current_authority_epoch_is_accepted():
    bridge = Bridge(actual_home="bacloud")
    result = await MeshApplication(bridge).revoke(
        ActorContext(endpoint_role="mesh", peer_node_id="bacloud"),
        dict(
            authority_node_id="bacloud",
            authority_epoch=4,
            logical_agent_id="la_home",
            work_session_id="ws_home",
            session_epoch=1,
        ),
    )
    assert result["ok"] and len(bridge.calls) == 1
