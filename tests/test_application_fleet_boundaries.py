"""Replication and management semantics share an actor-aware application boundary."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.fleet_control import FleetControlApplication
from terminal_mcp.application.mesh import MeshApplicationError
from terminal_mcp.application.replication import (
    FleetProjectionApplication,
    FleetSourceApplication,
    ReplicationApplication,
)
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    current_admission_context,
    reset_admission_context,
)
from terminal_mcp.core.persistent_policy import PersistentPolicyError
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


@pytest.mark.parametrize("name", ["replication.py", "fleet_control.py"])
def test_application_has_no_transport_imports(name):
    tree = ast.parse(
        (Path(__file__).parents[1] / "src/terminal_mcp/application" / name).read_text()
    )
    imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not any(
        name.startswith(("fastapi", "starlette", "mcp", "terminal_mcp.http", "terminal_mcp.mcp"))
        for name in imports
    )


@pytest.mark.parametrize("role", ["operator", "executor", "legacy"])
async def test_replication_and_control_require_verified_mesh_actor(role):
    target = SimpleNamespace(list_identities=AsyncMock())
    control = SimpleNamespace(authoritative_replication_snapshot=AsyncMock())
    actor = ActorContext(endpoint_role=role, peer_node_id="verified-peer")
    with pytest.raises(MeshApplicationError, match="invalid fleet peer"):
        await ReplicationApplication(target).read_identities(actor)
    with pytest.raises(MeshApplicationError, match="invalid fleet peer"):
        await FleetControlApplication(control).internal_state(actor)
    target.list_identities.assert_not_awaited()
    control.authoritative_replication_snapshot.assert_not_awaited()


@pytest.mark.parametrize(
    "payload,detail",
    [
        ({}, "session update payload is incomplete"),
        (
            {"agent_id": "agent", "source_instance_id": "node", "activity_at": "time", "intent": 1},
            "session update intent must be a string",
        ),
        (
            {
                "agent_id": "agent",
                "source_instance_id": "node",
                "activity_at": "time",
                "step": "one",
            },
            "session update step must be an integer",
        ),
    ],
)
async def test_replication_validation_matches_direct_and_http(payload, detail):
    receiver = AsyncMock()
    replication = SimpleNamespace(
        config=SimpleNamespace(instance_id="local"),
        authenticate=PeerAuth().authenticate,
        receive_session_update=receiver,
    )
    capability = ReplicationApplication(replication)
    with pytest.raises(MeshApplicationError) as rejected:
        await capability.update_session(mesh_actor(), payload=payload)
    assert rejected.value.kind == "invalid_request"
    assert rejected.value.detail == detail
    server = FastAPI()
    server.include_router(build_fleet_router(replication, application=capability))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/fleet/session-update", json=payload, headers=HEADERS
        )
    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    receiver.assert_not_awaited()


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


async def test_source_filter_projection_is_canonical_for_all_transports():
    query = AsyncMock(return_value={"items": []})
    capability = FleetSourceApplication(SimpleNamespace(query=query))
    actor = mesh_actor()
    await capability.query(actor, resource="tasks", namespace="ns", include_count=True)
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
    query.assert_awaited_once_with("tasks", **expected)
    query.reset_mock()
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


async def test_projection_epoch_recovery_is_application_owned():
    projection = SimpleNamespace(
        meta=AsyncMock(return_value={"projection_epoch": 3, "projection_seq": 42}),
        events=AsyncMock(return_value={"events": [{"seq": 41}]}),
    )
    capability = FleetProjectionApplication(projection)
    expected = {"projection_epoch": 3, "projection_seq": 42, "events": [], "reset_required": True}
    assert await capability.events(mesh_actor(), projection_epoch=2) == expected
    projection.events.assert_not_awaited()
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
        current = await client.get(
            "/internal/fleet/v1/projection/events",
            headers=HEADERS,
            params={"projection_epoch": 3},
        )
    assert current.json() == {"events": [{"seq": 41}], "reset_required": False}
    projection.events.assert_awaited_once_with(since=0, limit=100)


async def test_control_error_and_actor_binding_are_transport_independent():
    seen = []

    async def update(**kwargs):
        seen.append(current_admission_context())
        raise PersistentPolicyError("policy_in_use")

    capability = FleetControlApplication(SimpleNamespace(update_policy=update))
    actor = operator_actor()
    result = await capability.policy(
        actor,
        duration_seconds=1380,
        warning_after_seconds=1140,
        alert_after_seconds=1320,
        rearm_after_seconds=180,
        legacy_admission_enabled=False,
        expected_revision=1,
    )
    assert result == {"ok": False, "code": "policy_in_use", "error": "policy_in_use"}
    assert seen[0].principal_id == "operator"
    assert current_admission_context() is None


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


async def test_control_enrollment_hint_is_used_only_during_peer_authentication():
    authenticate = AsyncMock(return_value="verified-peer")
    apply = AsyncMock(return_value={"revision": 1})
    controller = SimpleNamespace(
        config=SimpleNamespace(instance_id="local"),
        authenticate_management_peer=authenticate,
        apply_replica=apply,
    )
    server = FastAPI()
    server.include_router(build_fleet_control_router(controller, PeerAuth()))
    state = {"control_node_id": "proposed-control"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/fleet/control/apply", headers=HEADERS, json={"state": state}
        )
    assert response.status_code == 200
    authenticate.assert_awaited_once_with(
        "peer",
        "Bearer mesh-test-key",
        first_apply_control_node_id="proposed-control",
        allow_detached_peer=False,
    )
    apply.assert_awaited_once_with(state, source_node_id="verified-peer")


async def test_control_cancellation_restores_prior_admission():
    controller = SimpleNamespace(snapshot=AsyncMock(side_effect=asyncio.CancelledError))
    actor = operator_actor()
    original = actor.admission()
    token = bind_admission_context(original)
    try:
        with pytest.raises(asyncio.CancelledError):
            await FleetControlApplication(controller).state(actor)
        assert current_admission_context() == original
    finally:
        reset_admission_context(token)
