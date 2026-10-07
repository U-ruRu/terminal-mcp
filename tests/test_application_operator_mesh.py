"""Application boundaries must behave identically without an HTTP server."""

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
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError
from terminal_mcp.application.operator import OperatorApplication
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    current_admission_context,
    reset_admission_context,
)
from terminal_mcp.core.persistent_policy import PersistentPolicyError
from terminal_mcp.http.persistent_fleet import build_persistent_fleet_router
from terminal_mcp.storage.persistent_agents import PersistentStoreError


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


@pytest.mark.parametrize("module", ["operator.py", "mesh.py"])
def test_capabilities_do_not_import_transport_modules(module):
    path = Path(__file__).parents[1] / "src/terminal_mcp/application" / module
    tree = ast.parse(path.read_text())
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module] + [
        item.name for n in ast.walk(tree) if isinstance(n, ast.Import) for item in n.names
    ]
    assert not any(
        name.startswith(("fastapi", "starlette", "mcp", "terminal_mcp.http", "terminal_mcp.mcp"))
        for name in imports
    )


@pytest.mark.parametrize("role", ["legacy", "executor", "coordinator", "operator"])
async def test_mesh_role_and_verified_peer_are_required(role):
    resolver = AsyncMock()
    app = MeshApplication(bridge(resolve_access_code=resolver))
    with pytest.raises(MeshApplicationError) as rejected:
        await app.access_resolve(
            ActorContext(endpoint_role=role, peer_node_id="peer"),
            {"requesting_instance_id": "peer", "access_code": "0000"},
        )
    assert rejected.value.kind == "unauthorized"
    resolver.assert_not_awaited()
    with pytest.raises(MeshApplicationError):
        await app.route(ActorContext(endpoint_role="mesh"), "slot")


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


@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "error", "cancel"])
async def test_mesh_forwarded_admission_is_scoped_and_restored(forwarded, outcome):
    seen = []

    async def start(_access):
        seen.append(current_admission_context())
        if outcome == "error":
            raise PersistentStoreError("session_not_active")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return {"ok": True}

    backend = SimpleNamespace(
        _resolve_access=AsyncMock(return_value={"authority_node_id": "local"}),
        _local_session_start_resolved=start,
    )
    app = MeshApplication(bridge(), backend)
    payload = {"requesting_instance_id": "peer", "access_code": "0000"}
    if forwarded:
        payload["forwarded_admission"] = {
            "principal_id": "forwarded",
            "credential_id": "oauth:forwarded",
            "auth_generation": 1,
            "auth_mode": "oauth",
            "transport": "mesh-forward",
            "scopes": ["terminal:read", "terminal:execute"],
        }
    outer = admission("outer")
    token = bind_admission_context(outer)
    try:
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await app.unified_session_start(mesh_actor(), payload)
        else:
            result = await app.unified_session_start(mesh_actor(), payload)
            assert result["ok"] is (outcome == "ok")
            if outcome == "error":
                assert result["code"] == "session_not_active"
        assert current_admission_context() == outer
    finally:
        reset_admission_context(token)
    assert len(seen) == 1
    assert seen[0].principal_id == "forwarded" if forwarded else seen[0] is None


async def test_wrong_authority_does_not_start_a_session():
    start = AsyncMock()
    app = MeshApplication(
        bridge(),
        SimpleNamespace(
            _resolve_access=AsyncMock(return_value={"authority_node_id": "remote"}),
            _local_session_start_resolved=start,
        ),
    )
    result = await app.unified_session_start(
        mesh_actor(), {"requesting_instance_id": "peer", "access_code": "0000"}
    )
    assert result == {"ok": False, "code": "authority_unavailable"}
    start.assert_not_awaited()


@pytest.mark.parametrize(
    "code,status",
    [
        ("slot_not_found", 404),
        ("message_not_found", 404),
        ("persistent_auth_required", 403),
        ("session_not_active", 409),
    ],
)
async def test_http_mesh_maps_only_transport_status_and_preserves_domain_error(code, status):
    target_bridge = bridge(issue_permit=AsyncMock(side_effect=PersistentStoreError(code)))
    capability = MeshApplication(target_bridge)
    payload = {"requesting_instance_id": "peer", "logical_agent_id": "slot"}
    with pytest.raises(MeshApplicationError) as rejected:
        await capability.issue_permit(mesh_actor(), payload)
    server = FastAPI()
    server.include_router(
        build_persistent_fleet_router(PeerAuthentication(), target_bridge, application=capability)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/fleet/persistent/permit", json=payload, headers=PEER_HEADERS
        )
    assert response.status_code == status
    assert response.json() == {"detail": rejected.value.detail}


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
        denied = await client.get("/internal/fleet/persistent/route/slot")
        assert denied.status_code == 401
        assert not captured
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


@pytest.mark.parametrize("outcome", ["ok", "error", "cancel"])
async def test_operator_binds_actor_and_restores_ambient_context(outcome):
    seen = []

    async def create(name):
        seen.append((name, current_admission_context()))
        if outcome == "error":
            raise RuntimeError("backend failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return {"ok": True, "display_name": name}

    app = OperatorApplication(SimpleNamespace(persistent=SimpleNamespace(slot_create=create)))
    outer = admission("outer")
    token = bind_admission_context(outer)
    try:
        if outcome == "ok":
            assert await app.slot_create(operator_actor(), display_name="slot") == {
                "ok": True,
                "display_name": "slot",
            }
        else:
            with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
                await app.slot_create(operator_actor(), display_name="slot")
        assert current_admission_context() == outer
    finally:
        reset_admission_context(token)
    assert seen[0][0] == "slot"
    assert seen[0][1].principal_id == "operator"


async def test_operator_policy_error_keeps_blockers_and_unavailable_contract():
    controller = SimpleNamespace(
        update=AsyncMock(side_effect=PersistentPolicyError("policy_in_use"))
    )
    app = OperatorApplication(SimpleNamespace(), controller)
    result = await app.policy_update(
        operator_actor(),
        duration_seconds=1380,
        warning_after_seconds=None,
        alert_after_seconds=None,
        rearm_after_seconds=None,
        legacy_admission_enabled=None,
    )
    assert result == {"ok": False, "code": "policy_in_use", "error": "policy_in_use"}
    assert await app.slot_list(operator_actor()) == {
        "ok": False,
        "code": "policy_incompatible",
        "error": "policy_incompatible",
    }


async def test_mesh_provider_registry_resolve_and_bind_are_peer_authenticated():
    resolver = AsyncMock(return_value="la_one")
    binder = AsyncMock(return_value="la_one")
    app = MeshApplication(
        bridge(resolve_provider_binding=resolver, bind_provider_binding=binder)
    )
    key = "a" * 64

    resolved = await app.provider_resolve(
        mesh_actor(),
        {"requesting_instance_id": "peer", "provider": "openai", "binding_key": key},
    )
    assert resolved == {"ok": True, "logical_agent_id": "la_one"}
    resolver.assert_awaited_once_with("openai", key)

    bound = await app.provider_bind(
        mesh_actor(),
        {
            "requesting_instance_id": "peer",
            "provider": "openai",
            "binding_key": key,
            "logical_agent_id": "la_one",
            "access_code": "1234",
            "principal_id": "usr_one",
        },
    )
    assert bound == {"ok": True, "logical_agent_id": "la_one"}
    binder.assert_awaited_once_with(
        "openai",
        key,
        "la_one",
        access_code="1234",
        principal_id="usr_one",
    )


async def test_mesh_provider_registry_rejects_spoofed_requesting_instance():
    resolver = AsyncMock(return_value="la_one")
    app = MeshApplication(bridge(resolve_provider_binding=resolver))
    with pytest.raises(MeshApplicationError, match="requesting instance mismatch"):
        await app.provider_resolve(
            mesh_actor(),
            {"requesting_instance_id": "other", "provider": "openai", "binding_key": "a" * 64},
        )
    resolver.assert_not_awaited()
