from types import SimpleNamespace

import httpx
import pytest

from terminal_mcp.core.persistent_fleet import PersistentFleetBridge
from terminal_mcp.storage.persistent_agents import PersistentStoreError


class Routes:
    def __init__(self):
        self.value = None

    async def route(self, agent):
        return self.value

    async def publish_route(self, agent, node, epoch, *, state="active", target_node_id=None):
        self.value = dict(
            logical_agent_id=agent,
            authority_node_id=node,
            authority_epoch=epoch,
            state=state,
            target_node_id=target_node_id,
        )
        return self.value


def make_bridge(change=None, status=200):
    routes = Routes()
    calls = []
    home = SimpleNamespace(instance_id="home", origin="https://home.test")
    config = SimpleNamespace(instance_id="peer", peers_by_id={"home": home})

    def request(req):
        calls.append(req)
        body = dict(
            logical_agent_id="la_foreign",
            authority_node_id="home",
            authority_epoch=3,
            state="active",
            target_node_id=None,
        )
        body.update(change or {})
        return httpx.Response(status, json={"ok": True, "route": body})

    transport = httpx.MockTransport(request)
    bridge = PersistentFleetBridge(
        config,
        None,
        None,
        None,
        None,
        control_store=routes,
        control_node_id="registry",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    bridge._headers = lambda peer: {
        "X-Terminal-MCP-Peer": "peer",
        "Authorization": "Bearer fixture",
    }

    async def registry(agent):
        assert agent == "la_foreign"
        return dict(logical_agent_id=agent, authority_node_id="home", status="active")

    bridge.get_access_slot = registry
    return bridge, routes, calls


@pytest.mark.asyncio
async def test_first_contact_resolves_registry_home_and_caches_verified_route():
    bridge, routes, calls = make_bridge()
    result = await bridge.route_info("la_foreign")
    assert result and result["authority_node_id"] == "home"
    assert result["authority_epoch"] == 3
    assert len(calls) == 1
    assert str(calls[0].url) == "https://home.test/internal/fleet/persistent/route/la_foreign"
    assert await bridge.route_info("la_foreign") == result
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"logical_agent_id": "other"},
        {"authority_node_id": "untrusted"},
        {"authority_epoch": True},
        {"authority_epoch": 0},
        {"authority_epoch": "3"},
        {"state": "bogus"},
    ],
)
async def test_first_contact_rejects_mismatched_or_malformed_authority_proof(change):
    bridge, routes, _ = make_bridge(change)
    with pytest.raises(PersistentStoreError, match="authority_unavailable"):
        await bridge.route_info("la_foreign")
    assert routes.value is None


@pytest.mark.asyncio
async def test_first_contact_preserves_nonactive_route_state():
    bridge, _, _ = make_bridge({"state": "recovery_required"})
    result = await bridge.route_info("la_foreign")
    assert result["state"] == "recovery_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 500])
async def test_first_contact_unreachable_authority_does_not_invent_route(status):
    bridge, routes, _ = make_bridge(status=status)
    with pytest.raises(PersistentStoreError, match="authority_unavailable"):
        await bridge.route_info("la_foreign")
    assert routes.value is None
