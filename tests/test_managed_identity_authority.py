from __future__ import annotations

from dataclasses import replace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.managed_identity import (
    ManagedGrantAuthorizer,
    ManagedProviderResolver,
)
from terminal_mcp.core.managed_sessions import ManagedOperation, ManagedSessionError
from terminal_mcp.core.persistent_agents import PersistentSlot


class Repository:
    authority_node_id = "home"

    def __init__(self):
        self.bound = "la_one"
        self.bind_calls = []
        self.slot = PersistentSlot(
            logical_agent_id="la_one",
            display_name="Stable-Agent",
            state="armed",
            authority_node_id="home",
            authority_epoch=3,
            slot_revision=7,
            selector_generation=1,
            auth_generation=2,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

    async def resolve_provider(self, identity):
        return self.bound if identity.provider == "openai" else None

    async def bind_provider(self, identity, logical_agent_id, *, principal_id):
        self.bind_calls.append((identity, logical_agent_id, principal_id))
        if self.bound not in {None, logical_agent_id}:
            raise ManagedSessionError("identity_binding_conflict")
        self.bound = logical_agent_id
        return logical_agent_id

    async def get_slot(self, logical_agent_id):
        return self.slot if logical_agent_id == self.slot.logical_agent_id else None


class Routes:
    def __init__(self):
        self.value = {
            "logical_agent_id": "la_one",
            "authority_node_id": "home",
            "authority_epoch": 3,
            "routing_revision": 9,
            "state": "active",
            "target_node_id": None,
        }

    async def route_info(self, logical_agent_id):
        return self.value if logical_agent_id == "la_one" else None


class Authority:
    def __init__(self):
        self.slot = {
            "logical_agent_id": "la_one",
            "public_name": "Agent-17",
            "authority_node_id": "home",
            "status": "active",
        }
        self.grants = [
            {
                "grant_id": "grt_one",
                "principal_id": "usr_one",
                "client_id": "cli_one",
                "resources": ["node:home"],
                "scopes": ["terminal:read", "terminal:execute"],
                "role": "executor",
            }
        ]

    async def access_slot(self, logical_agent_id):
        return self.slot if logical_agent_id == "la_one" else None

    async def active_grants(self, principal_id):
        return list(self.grants) if principal_id == "usr_one" else []


def actor(*, generation=2, role="executor"):
    return ActorContext(
        principal_id="usr_one",
        credential_id="oauth:cli_one",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=generation,
        auth_mode="oauth",
        provider="openai",
        node_id="edge",
        endpoint_role=role,
    )


def metadata():
    return {
        "openai/subject": "opaque-user",
        "openai/session": "opaque-conversation",
        "openai/organization": "opaque-org",
    }


@pytest.mark.asyncio
async def test_provider_resolution_is_identity_only_and_uses_authoritative_route():
    resolved = await ManagedProviderResolver(Repository(), routes=Routes()).resolve(
        actor(), "openai", metadata()
    )
    assert resolved.logical_agent_id == "la_one"
    assert resolved.authority_node_id == "home"
    assert resolved.work_session_id is None
    assert resolved.principal_id == "usr_one"


@pytest.mark.asyncio
async def test_provider_binding_requires_authenticated_principal():
    repository = Repository()
    repository.bound = None
    resolver = ManagedProviderResolver(repository, routes=Routes())
    with pytest.raises(ManagedSessionError, match="persistent_auth_required"):
        await resolver.bind_existing(
            ActorContext(provider="openai"), "openai", metadata(), "la_one"
        )
    assert repository.bound is None


@pytest.mark.asyncio
async def test_authorizer_uses_existing_principal_client_scope_resource_and_route():
    grant = await ManagedGrantAuthorizer(Repository(), Authority(), routes=Routes()).authorize(
        actor(), "la_one", ManagedOperation.COMMAND_RUN
    )
    assert grant.logical_agent_id == "la_one"
    assert grant.authority_node_id == "home"
    assert grant.authority_epoch == 3
    assert grant.public_name == "Agent-17"
    assert grant.principal_id == "usr_one"
    assert grant.auth_generation == 2
    assert not grant.operator


@pytest.mark.asyncio
async def test_read_grant_cannot_authorize_task_creation():
    authority = Authority()
    authority.grants[0]["scopes"] = ["terminal:read"]
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
            actor(), "la_one", ManagedOperation.TASK_CREATE
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", "another-client"),
        ("resources", ["node:other"]),
    ],
)
async def test_authorizer_does_not_broaden_principal_grants(field, value):
    authority = Authority()
    authority.grants[0][field] = value
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
            actor(), "la_one", ManagedOperation.COMMAND_RUN
        )


@pytest.mark.asyncio
async def test_operator_grant_authorizes_window_operation():
    authority = Authority()
    authority.grants[0]["role"] = "operator"
    grant = await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
        actor(role="operator"), "la_one", ManagedOperation.OPERATOR_WINDOW
    )
    assert grant.operator


@pytest.mark.asyncio
async def test_authorizer_rejects_stale_auth_generation():
    with pytest.raises(ManagedSessionError, match="auth_generation_mismatch") as error:
        await ManagedGrantAuthorizer(Repository(), Authority(), routes=Routes()).authorize(
            actor(generation=1), "la_one", ManagedOperation.COMMAND_RUN
        )
    assert error.value.return_to_chat


@pytest.mark.asyncio
async def test_authorizer_rejects_stale_access_slot_authority():
    authority = Authority()
    authority.slot = {**authority.slot, "authority_node_id": "stale-home"}
    with pytest.raises(ManagedSessionError, match="access_slot_unavailable"):
        await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
            actor(), "la_one", ManagedOperation.COMMAND_RUN
        )


@pytest.mark.asyncio
async def test_provider_resolution_requires_matching_trusted_provider():
    with pytest.raises(ManagedSessionError, match="identity_mismatch"):
        await ManagedProviderResolver(Repository(), routes=Routes()).resolve(
            replace(actor(), provider="custom"), "openai", metadata()
        )


class FleetIdentityRoutes(Routes):
    def __init__(self):
        super().__init__()
        self.provider_bound = "la_one"
        self.provider_bind_calls = []

    async def resolve_provider_binding(self, provider, binding_key):
        self.provider_resolve = (provider, binding_key)
        return self.provider_bound

    async def bind_provider_binding(
        self,
        provider,
        binding_key,
        logical_agent_id,
        *,
        access_code=None,
        principal_id=None,
        bootstrap=False,
    ):
        self.provider_bind_calls.append(
            (provider, binding_key, logical_agent_id, access_code, principal_id, bootstrap)
        )
        self.provider_bound = logical_agent_id
        return logical_agent_id


@pytest.mark.asyncio
async def test_fleet_provider_binding_requires_access_code():
    repository = Repository()
    repository.bound = None
    resolver = ManagedProviderResolver(repository, routes=FleetIdentityRoutes())
    with pytest.raises(ManagedSessionError, match="access_code_required"):
        await resolver.bind_existing(actor(), "openai", metadata(), "la_one")


@pytest.mark.asyncio
async def test_fleet_provider_bootstrap_is_server_authorized_without_access_code():
    repository = Repository()
    repository.bound = None
    routes = FleetIdentityRoutes()
    resolver = ManagedProviderResolver(repository, routes=routes)

    resolved = await resolver.bind_existing(actor(), "openai", metadata(), "la_one", bootstrap=True)

    assert resolved.logical_agent_id == "la_one"
    assert routes.provider_bind_calls[0][2:] == ("la_one", None, "usr_one", True)


class RemoteRepository(Repository):
    authority_node_id = "edge"

    async def get_slot(self, logical_agent_id):
        return None


@pytest.mark.asyncio
async def test_fleet_provider_identity_resolves_without_local_slot_replica():
    repository = RemoteRepository()
    repository.bound = None
    routes = FleetIdentityRoutes()

    resolved = await ManagedProviderResolver(repository, routes=routes).resolve(
        actor(), "openai", metadata()
    )

    assert resolved.logical_agent_id == "la_one"
    assert resolved.authority_node_id == "home"
    assert repository.bound is None
