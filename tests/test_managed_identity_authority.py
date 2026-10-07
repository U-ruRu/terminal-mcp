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
async def test_provider_resolution_requires_existing_binding_and_current_fleet_route():
    repository = Repository()
    repository.bound = None
    with pytest.raises(ManagedSessionError, match="identity_not_bound"):
        await ManagedProviderResolver(repository, routes=Routes()).resolve(
            actor(), "openai", metadata()
        )

    repository.bound = "la_one"
    routes = Routes()
    routes.value["authority_epoch"] = 4
    resolved = await ManagedProviderResolver(repository, routes=routes).resolve(
        actor(), "openai", metadata()
    )
    assert resolved.logical_agent_id == "la_one"
    assert resolved.authority_node_id == "home"
    with pytest.raises(ManagedSessionError, match="authority_unavailable"):
        await ManagedGrantAuthorizer(repository, Authority(), routes=routes).authorize(
            resolved, "la_one", ManagedOperation.COMMAND_RUN
        )


@pytest.mark.asyncio
async def test_provider_binding_requires_independent_authenticated_slot_proof():
    repository = Repository()
    repository.bound = None
    resolver = ManagedProviderResolver(repository, routes=Routes())

    with pytest.raises(ManagedSessionError, match="persistent_auth_required"):
        await resolver.bind_existing(
            ActorContext(provider="openai"), "openai", metadata(), "la_one"
        )
    assert repository.bind_calls == []

    resolved = await resolver.bind_existing(actor(), "openai", metadata(), "la_one")
    assert resolved.logical_agent_id == "la_one"
    assert resolved.authority_node_id == "home"
    assert repository.bind_calls[0][1:] == ("la_one", "usr_one")


@pytest.mark.asyncio
async def test_binding_provider_does_not_grant_managed_operation_authorization():
    repository = Repository()
    repository.bound = None
    resolver = ManagedProviderResolver(repository, routes=Routes())
    resolved = await resolver.bind_existing(actor(), "openai", metadata(), "la_one")

    authority = Authority()
    authority.grants = []
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await ManagedGrantAuthorizer(repository, authority, routes=Routes()).authorize(
            resolved, "la_one", ManagedOperation.COMMAND_RUN
        )


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
async def test_read_and_execute_grant_scopes_are_independent():
    authority = Authority()
    authority.grants[0]["scopes"] = ["terminal:read"]
    authorizer = ManagedGrantAuthorizer(Repository(), authority, routes=Routes())
    await authorizer.authorize(actor(), "la_one", ManagedOperation.TASK_LIST)
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await authorizer.authorize(actor(), "la_one", ManagedOperation.TASK_CREATE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", "another-client"),
        ("resources", ["node:other"]),
        ("resources", ["some-unrelated-resource"]),
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
async def test_operator_operation_requires_operator_grant():
    authority = Authority()
    authorizer = ManagedGrantAuthorizer(Repository(), authority, routes=Routes())
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await authorizer.authorize(
            actor(role="operator"), "la_one", ManagedOperation.OPERATOR_WINDOW
        )
    authority.grants[0]["role"] = "operator"
    grant = await authorizer.authorize(
        actor(role="operator"), "la_one", ManagedOperation.OPERATOR_WINDOW
    )
    assert grant.operator


@pytest.mark.asyncio
async def test_authorizer_fences_auth_generation_and_access_slot_authority():
    with pytest.raises(ManagedSessionError, match="auth_generation_mismatch") as error:
        await ManagedGrantAuthorizer(Repository(), Authority(), routes=Routes()).authorize(
            actor(generation=1), "la_one", ManagedOperation.COMMAND_RUN
        )
    assert error.value.return_to_chat

    authority = Authority()
    authority.slot = {**authority.slot, "authority_node_id": "stale-home"}
    with pytest.raises(ManagedSessionError, match="access_slot_unavailable"):
        await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
            actor(), "la_one", ManagedOperation.COMMAND_RUN
        )


@pytest.mark.asyncio
async def test_authorizer_is_auth_mode_agnostic_but_requires_explicit_client_grant():
    authority = Authority()
    bearer = replace(actor(), auth_mode="bearer", credential_id="bearer:cli_one")
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
            bearer, "la_one", ManagedOperation.COMMAND_RUN
        )
    authority.grants[0]["client_id"] = "bearer:cli_one"
    grant = await ManagedGrantAuthorizer(Repository(), authority, routes=Routes()).authorize(
        bearer, "la_one", ManagedOperation.COMMAND_RUN
    )
    assert grant.principal_id == "usr_one"


@pytest.mark.asyncio
async def test_provider_and_standalone_authority_must_match_trusted_context():
    mismatched_provider = replace(actor(), provider="custom")
    with pytest.raises(ManagedSessionError, match="identity_mismatch"):
        await ManagedProviderResolver(Repository(), routes=Routes()).resolve(
            mismatched_provider, "openai", metadata()
        )

    repository = Repository()
    repository.slot = replace(repository.slot, authority_node_id="other-node")
    with pytest.raises(ManagedSessionError, match="authority_unavailable"):
        await ManagedProviderResolver(repository).resolve(actor(), "openai", metadata())



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
        access_code,
        principal_id=None,
    ):
        self.provider_bind_calls.append(
            (provider, binding_key, logical_agent_id, access_code, principal_id)
        )
        self.provider_bound = logical_agent_id
        return logical_agent_id


@pytest.mark.asyncio
async def test_fleet_provider_registry_resolves_identity_before_local_replica():
    repository = Repository()
    repository.bound = None
    routes = FleetIdentityRoutes()
    resolved = await ManagedProviderResolver(repository, routes=routes).resolve(
        actor(), "openai", metadata()
    )
    assert resolved.logical_agent_id == "la_one"
    assert hasattr(routes, "provider_resolve")
    assert repository.bound is None


@pytest.mark.asyncio
async def test_fleet_provider_bind_requires_access_code_proof():
    repository = Repository()
    repository.bound = None
    routes = FleetIdentityRoutes()
    resolver = ManagedProviderResolver(repository, routes=routes)

    with pytest.raises(ManagedSessionError, match="access_code_required"):
        await resolver.bind_existing(actor(), "openai", metadata(), "la_one")

    resolved = await resolver.bind_existing(
        actor(), "openai", metadata(), "la_one", access_code="1234"
    )
    assert resolved.logical_agent_id == "la_one"
    assert routes.provider_bind_calls[0][2:] == ("la_one", "1234", "usr_one")


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
    with pytest.raises(ManagedSessionError, match="slot_not_found"):
        await ManagedGrantAuthorizer(repository, Authority(), routes=routes).authorize(
            resolved, "la_one", ManagedOperation.COMMAND_RUN
        )


@pytest.mark.asyncio
async def test_remote_provider_binding_uses_control_authority_without_local_replica():
    repository = RemoteRepository()
    repository.bound = None
    routes = FleetIdentityRoutes()
    resolver = ManagedProviderResolver(repository, routes=routes)

    resolved = await resolver.bind_existing(
        actor(), "openai", metadata(), "la_one", access_code="1234"
    )

    assert resolved.logical_agent_id == "la_one"
    assert resolved.authority_node_id == "home"
    assert routes.provider_bind_calls[0][2:] == ("la_one", "1234", "usr_one")
    assert repository.bind_calls == []
