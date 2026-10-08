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
@pytest.mark.parametrize("action", ["revoke", "drain"])
async def test_authenticated_non_authority_cannot_mutate_foreign_session(action):
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
        await getattr(app, action)(actor, payload)
    assert bridge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "drain"])
async def test_authenticated_home_can_mutate_its_own_exact_session(action):
    bridge = Bridge(actual_home="bacloud")
    app = MeshApplication(bridge)
    actor = ActorContext(endpoint_role="mesh", peer_node_id="bacloud", node_id="secondary")
    payload = {
        "authority_node_id": "bacloud",
        "logical_agent_id": "la_home",
        "work_session_id": "ws_home",
        "session_epoch": 1,
        "hard_expires_at": "2026-10-08T02:00:00Z",
        "reason": "review",
    }
    result = await getattr(app, action)(actor, payload)
    assert result["ok"] is True
    assert [call[0] for call in bridge.calls] == [action]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "drain"])
@pytest.mark.parametrize("epoch", [0, 3, 5, True, "4"])
async def test_session_control_rejects_stale_or_malformed_authority_epoch(action, epoch):
    bridge = Bridge(actual_home="bacloud")
    app = MeshApplication(bridge)
    actor = ActorContext(endpoint_role="mesh", peer_node_id="bacloud")
    with pytest.raises(MeshApplicationError):
        await getattr(app, action)(
            actor,
            dict(
                authority_node_id="bacloud",
                authority_epoch=epoch,
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
