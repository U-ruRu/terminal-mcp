"""Replication and management semantics share an actor-aware application boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.replication import (
    FleetProjectionApplication,
    FleetSourceApplication,
)
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    current_admission_context,
)
from terminal_mcp.http.fleet import build_fleet_router
from terminal_mcp.http.fleet_control import build_fleet_control_router
from terminal_mcp.http.fleet_v1 import (
    build_fleet_v1_projection_router,
    build_fleet_v1_source_router,
)


def mesh_actor():
    return ActorContext(
        transport="mesh", endpoint_role="mesh", node_id="local", peer_node_id="verified-peer"
    )


def operator_actor():
    return ActorContext.from_admission(
        VerifiedAdmissionContext(
            principal_id="operator",
            credential_id="oauth:operator",
            auth_generation=1,
            auth_mode="oauth",
            transport="http",
            scopes=frozenset({"terminal:read", "terminal:execute"}),
        ),
        endpoint_role="operator",
        node_id="local",
        transport="http",
    )


class PeerAuth:
    config = SimpleNamespace(instance_id="local")

    def authenticate(self, peer_id, authorization):
        if peer_id == "peer" and authorization == "Bearer mesh-test-key":
            return SimpleNamespace(instance_id="verified-peer")
        return None


HEADERS = {"X-Terminal-MCP-Peer": "peer", "Authorization": "Bearer mesh-test-key"}


async def test_replication_peer_attribution_cannot_come_from_payload():
    receiver = AsyncMock(return_value=True)
    replication = SimpleNamespace(
        config=SimpleNamespace(instance_id="local"),
        authenticate=PeerAuth().authenticate,
        receive_finish=receiver,
    )
    server = FastAPI()
    server.include_router(build_fleet_router(replication))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/fleet/session-finish",
            headers=HEADERS,
            json={
                "agent_id": "agent",
                "ended_at": "time",
                "reason": "done",
                "authenticated_peer_id": "forged",
            },
        )
    assert response.json() == {"ok": True, "changed": True}
    receiver.assert_awaited_once_with(
        "agent", "time", "done", authenticated_peer_id="verified-peer"
    )


async def test_source_filter_projection_forwards_canonical_filters_over_http():
    query = AsyncMock(return_value={"items": []})
    capability = FleetSourceApplication(SimpleNamespace(query=query))
    expected = {
        "cursor": None,
        "limit": 100,
        "q": None,
        "filters": {"namespace": "ns"},
        "as_of": None,
        "through_seq": None,
        "include_count": True,
        "include_facets": False,
    }
    server = FastAPI()
    server.include_router(build_fleet_v1_source_router(None, PeerAuth(), application=capability))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.get(
            "/internal/fleet/v1/source/query/tasks",
            headers=HEADERS,
            params={"namespace": "ns", "include_count": "true"},
        )
    assert response.json() == {"items": []}
    query.assert_awaited_once_with("tasks", **expected)


async def test_projection_epoch_recovery_maps_over_http():
    projection = SimpleNamespace(
        meta=AsyncMock(return_value={"projection_epoch": 3, "projection_seq": 42}),
        events=AsyncMock(return_value={"events": [{"seq": 41}]}),
    )
    capability = FleetProjectionApplication(projection)
    expected = {"projection_epoch": 3, "projection_seq": 42, "events": [], "reset_required": True}
    server = FastAPI()
    server.include_router(
        build_fleet_v1_projection_router(projection, PeerAuth(), application=capability)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.get(
            "/internal/fleet/v1/projection/events",
            headers=HEADERS,
            params={"projection_epoch": 2},
        )
        assert response.json() == expected


async def test_control_forwarding_uses_auth_result_not_supplied_peer_header():
    seen = []

    async def authenticate(peer_id, _authorization, **_kwargs):
        assert peer_id == "peer"
        return "verified-peer"

    async def execute(operation, payload, authenticated_peer_id):
        seen.append((operation, payload, authenticated_peer_id, current_admission_context()))
        return {"revision": 5}

    controller = SimpleNamespace(
        config=SimpleNamespace(instance_id="local"),
        authenticate_management_peer=authenticate,
        execute_forwarded=execute,
    )
    server = FastAPI()
    server.include_router(build_fleet_control_router(controller, PeerAuth()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/fleet/control/mutate/rename",
            headers=HEADERS,
            json={"payload": {"display_name": "new", "authenticated_peer_id": "forged"}},
        )
    assert response.json() == {"ok": True, "control": {"revision": 5}}
    assert seen[0][0] == "rename"
    assert seen[0][1]["authenticated_peer_id"] == "forged"
    assert seen[0][2] == "verified-peer"
    assert seen[0][3] is None
