"""Replication and management semantics share an actor-aware application boundary."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.access_mesh_operator import AccessMeshOperator
from terminal_mcp.application.access_mesh_registry import (
    REGISTRY_BINDING,
    AccessRegistryApplication,
)
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.replication import (
    FleetProjectionApplication,
    FleetSourceApplication,
)
from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    current_admission_context,
)
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


@pytest.fixture
def access_registry():
    registry = AccessRegistryApplication()
    registry.store = SimpleNamespace(
        namespace_id="mesh-x",
        local_node_id="member",
        registry_authority_id="authority",
        trusted_issuers=frozenset({"member", "authority"}),
        assert_registry_ready=Mock(),
        assert_registry_available=Mock(),
    )
    registry.replication = SimpleNamespace(registry_request=AsyncMock(return_value={"ok": True}))
    registry.connection_key = Mock(side_effect=AccessMeshError("identity_metadata_missing"))
    registry.issuer_session = AsyncMock()
    return registry


@pytest.fixture
def operator_registry_request():
    return {
        "namespace_id": "mesh-x",
        "operation": "slots",
        "arguments": {"limit": 20, "cursor": None},
        "actor": {
            "principal_id": "operator",
            "credential_id": "oauth:operator",
            "scopes": ["terminal:execute", "terminal:read"],
            "auth_generation": 1,
            "auth_mode": "oauth",
            "provider": None,
            "provider_metadata": {},
            "transport": "http",
            "endpoint_role": "operator",
            "contract_version": 1,
        },
        "connection_key": None,
    }


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("slots", {"limit": 20, "cursor": None}),
        ("get", {"slot_id": "slot"}),
        ("defaults", {}),
        ("mutate", {"action": "create", "idempotency_key": "create", "mode": "temporary"}),
    ],
)
async def test_operator_registry_proxy_does_not_require_agent_metadata(
    access_registry, operator_registry_request, operation, arguments
):
    operator_registry_request.update(operation=operation, arguments=arguments)

    result = await access_registry.proxy_registry(operator_actor(), operation, arguments)

    assert result == {"ok": True}
    access_registry.replication.registry_request.assert_awaited_once_with(
        operator_registry_request
    )
    access_registry.connection_key.assert_not_called()


@pytest.mark.parametrize("role,binding", [("operator", None), ("access", "a" * 64)])
async def test_registry_dispatch_uses_the_admitted_role_binding(
    access_registry, operator_registry_request, monkeypatch, role, binding
):
    access_registry.store.local_node_id = "authority"
    arguments = (
        {"action": "create", "idempotency_key": "create", "mode": "temporary"}
        if role == "operator" else {"action": "start", "session_number": "1234"}
    )
    operator_registry_request.update(
        operation="mutate" if role == "operator" else "session",
        arguments=arguments,
        connection_key=binding,
    )
    operator_registry_request["actor"]["endpoint_role"] = role
    if role == "access":
        operator_registry_request["actor"].update(
            provider="codex", provider_metadata={"threadId": "verified-thread"}
        )
    observed_bindings = []

    async def dispatched(actor, **values):
        observed_bindings.append(REGISTRY_BINDING.get())
        return {"ok": True}

    handler = AsyncMock(side_effect=dispatched)
    monkeypatch.setattr(AccessMeshOperator, "mutate", handler)
    access_registry.issuer_session = handler
    outer_binding = "b" * 64
    token = REGISTRY_BINDING.set(outer_binding)
    try:
        result = await access_registry.accept_registry_request(
            operator_registry_request, authenticated_peer_id="member"
        )

        assert REGISTRY_BINDING.get() == outer_binding
    finally:
        REGISTRY_BINDING.reset(token)

    assert result == {"ok": True}
    handler.assert_awaited_once()
    delegated_actor = handler.await_args.args[0]
    assert delegated_actor.admission() == operator_actor().admission()
    assert delegated_actor.endpoint_role == role
    assert handler.await_args.kwargs == arguments
    assert observed_bindings == [binding]


@pytest.mark.parametrize(
    "operation,role,binding,code",
    [
        ("mutate", "operator", "a" * 64, "invalid_request"),
        ("mutate", "access", None, "access_denied"),
        ("session", "access", None, "invalid_request"),
        ("session", "operator", "a" * 64, "access_denied"),
    ],
)
async def test_registry_request_rejects_crossed_operator_and_agent_bindings(
    access_registry, operator_registry_request, operation, role, binding, code
):
    access_registry.store.local_node_id = "authority"
    operator_registry_request.update(
        operation=operation,
        arguments=(
            {"action": "create", "idempotency_key": "create", "mode": "temporary"}
            if operation == "mutate" else {"action": "start"}
        ),
        connection_key=binding,
    )
    operator_registry_request["actor"]["endpoint_role"] = role

    with pytest.raises(AccessMeshError, match=f"^{code}$"):
        await access_registry.accept_registry_request(
            operator_registry_request, authenticated_peer_id="member"
        )

    access_registry.issuer_session.assert_not_awaited()


async def test_agent_registry_proxy_still_requires_connection_identity(access_registry):
    actor = replace(operator_actor(), endpoint_role="access")

    with pytest.raises(AccessMeshError, match="^identity_metadata_missing$"):
        await access_registry.proxy_registry(actor, "session", {"action": "start"})

    access_registry.connection_key.assert_called_once_with(actor)
    access_registry.replication.registry_request.assert_not_awaited()


class PeerAuth:
    config = SimpleNamespace(instance_id="local")

    def authenticate(self, peer_id, authorization):
        if peer_id == "peer" and authorization == "Bearer mesh-test-key":
            return SimpleNamespace(instance_id="verified-peer")
        return None


HEADERS = {"X-Terminal-MCP-Peer": "peer", "Authorization": "Bearer mesh-test-key"}


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
