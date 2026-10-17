"""Application boundaries must behave identically without an HTTP server."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError
from terminal_mcp.application.operator import OperatorApplication
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    current_admission_context,
)
from terminal_mcp.http.persistent_fleet import build_persistent_fleet_router


def admission(principal="operator"):
    return VerifiedAdmissionContext(
        principal_id=principal,
        credential_id=f"oauth:{principal}",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="http",
        auth_mode="oauth",
    )


def operator_actor():
    return ActorContext.from_admission(admission(), endpoint_role="operator", node_id="local")


def mesh_actor():
    return ActorContext(
        transport="mesh", endpoint_role="mesh", node_id="local", peer_node_id="peer"
    )


def bridge(**kwargs):
    return SimpleNamespace(config=SimpleNamespace(instance_id="local"), **kwargs)


class PeerAuthentication:
    def authenticate(self, peer_id, authorization):
        if peer_id == "peer" and authorization == "Bearer mesh-test-key":
            return SimpleNamespace(instance_id="peer")
        return None


PEER_HEADERS = {"X-Terminal-MCP-Peer": "peer", "Authorization": "Bearer mesh-test-key"}


async def test_mesh_request_cannot_override_authenticated_peer():
    resolver = AsyncMock()
    app = MeshApplication(bridge(resolve_access_code=resolver))
    with pytest.raises(MeshApplicationError) as rejected:
        await app.access_resolve(
            mesh_actor(), {"requesting_instance_id": "other-peer", "access_code": "0000"}
        )
    assert rejected.value.kind == "invalid_request"
    assert rejected.value.detail == "requesting instance mismatch"
    resolver.assert_not_awaited()


async def test_mesh_status_batch_uses_bounded_local_logical_ids_and_forwarded_admission():
    seen = []

    async def statuses(access):
        seen.append((access, current_admission_context()))
        return [{"logical_agent_id": "la_one", "session_state": "active"}]

    backend = SimpleNamespace(access_active_statuses=statuses)
    app = MeshApplication(bridge(), backend)
    payload = {
        "requesting_instance_id": "peer",
        "access": [
            {"logical_agent_id": "la_one", "authority_node_id": "local", "public_name": "spoof"},
            {"logical_agent_id": "la_two", "authority_node_id": "local"},
        ],
        "forwarded_admission": {
            "principal_id": "forwarded",
            "credential_id": "oauth:forwarded",
            "auth_generation": 1,
            "auth_mode": "oauth",
            "transport": "mesh-forward",
            "scopes": ["terminal:read", "terminal:execute"],
        },
    }

    result = await app.unified_session_status_batch(mesh_actor(), payload)

    assert result == {
        "ok": True,
        "result": {"sessions": [{"logical_agent_id": "la_one", "session_state": "active"}]},
    }
    assert seen[0][0] == [
        {"logical_agent_id": "la_one", "authority_node_id": "local"},
        {"logical_agent_id": "la_two", "authority_node_id": "local"},
    ]
    assert seen[0][1].principal_id == "forwarded"


async def test_mesh_status_batch_rejects_foreign_authority():
    statuses = AsyncMock()
    app = MeshApplication(bridge(), SimpleNamespace(access_active_statuses=statuses))

    with pytest.raises(MeshApplicationError) as rejected:
        await app.unified_session_status_batch(
            mesh_actor(),
            {
                "requesting_instance_id": "peer",
                "access": [{"logical_agent_id": "la_one", "authority_node_id": "remote"}],
            },
        )

    assert rejected.value.kind == "invalid_request"
    statuses.assert_not_awaited()


async def test_http_peer_identity_is_derived_from_auth_not_body():
    captured = []

    async def route(actor, logical_agent_id):
        captured.append((actor, logical_agent_id))
        return {"ok": True}

    server = FastAPI()
    server.include_router(
        build_persistent_fleet_router(
            PeerAuthentication(), bridge(), application=SimpleNamespace(route=route)
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.get(
            "/internal/fleet/persistent/route/slot",
            headers=PEER_HEADERS,
            params={"peer_node_id": "forged", "node_id": "forged"},
        )
    assert response.status_code == 200
    actor, logical_agent_id = captured[0]
    assert actor.peer_node_id == "peer"
    assert actor.node_id == "local"
    assert actor.endpoint_role == "mesh"
    assert logical_agent_id == "slot"


async def test_operator_cannot_be_called_as_an_agent():
    create = AsyncMock()
    app = OperatorApplication(SimpleNamespace(persistent=SimpleNamespace(slot_create=create)))
    with pytest.raises(PermissionError, match="operator_role_required"):
        await app.slot_create(ActorContext(endpoint_role="executor"), display_name="slot")
    create.assert_not_awaited()
