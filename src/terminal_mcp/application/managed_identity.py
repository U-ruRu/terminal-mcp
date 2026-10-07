"""Provider resolution, authoritative routing and managed-slot authorization.

Provider metadata is only identity evidence. This module deliberately keeps three
independent checks in sequence: resolve an existing provider binding, resolve the
canonical authority route, then authorize the verified principal/client against
AuthFoundation grants. It never creates a slot or provider binding implicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.managed_sessions import ManagedSlotGrant
from terminal_mcp.core.managed_sessions import (
    OPERATOR_OPERATIONS,
    READ_OPERATIONS,
    ManagedOperation,
    ManagedSessionError,
)
from terminal_mcp.core.persistent_agents import PersistentSlot
from terminal_mcp.core.provider_identity import ProviderIdentity, ProviderIdentityRegistry


class ManagedIdentityRepository(Protocol):
    async def resolve_provider(self, identity: ProviderIdentity) -> str | None: ...

    async def bind_provider(
        self, identity: ProviderIdentity, logical_agent_id: str, *, principal_id: str
    ) -> str: ...

    async def get_slot(self, logical_agent_id: str) -> PersistentSlot | None: ...


class ManagedAccessAuthority(Protocol):
    async def active_grants(self, principal_id: str) -> list[dict]: ...

    async def access_slot(self, logical_agent_id: str) -> dict | None: ...


class ManagedFleetRoutes(Protocol):
    async def route_info(self, logical_agent_id: str) -> dict | None: ...

    async def resolve_provider_binding(self, provider: str, binding_key: str) -> str | None: ...

    async def bind_provider_binding(
        self,
        provider: str,
        binding_key: str,
        logical_agent_id: str,
        *,
        access_code: str,
        principal_id: str | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ManagedAuthorityRoute:
    logical_agent_id: str
    authority_node_id: str
    authority_epoch: int


class ManagedAuthorityRouter:
    """Resolve canonical Fleet identity routes; local authorization still needs a local slot."""

    def __init__(
        self, repository: ManagedIdentityRepository, routes: ManagedFleetRoutes | None = None
    ):
        self.repository = repository
        self.routes = routes

    @staticmethod
    def _decode_route(logical_agent_id: str, raw: Mapping[str, object]) -> ManagedAuthorityRoute:
        try:
            route = ManagedAuthorityRoute(
                logical_agent_id=str(raw["logical_agent_id"]),
                authority_node_id=str(raw["authority_node_id"]),
                authority_epoch=int(raw["authority_epoch"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManagedSessionError("authority_unavailable") from exc
        if (
            raw.get("state") != "active"
            or route.logical_agent_id != logical_agent_id
            or not route.authority_node_id
            or route.authority_epoch < 1
        ):
            raise ManagedSessionError("authority_unavailable")
        return route

    async def identity_route(self, logical_agent_id: str) -> ManagedAuthorityRoute:
        if self.routes is not None:
            raw = await self.routes.route_info(logical_agent_id)
            if not isinstance(raw, Mapping):
                raise ManagedSessionError("authority_unavailable")
            return self._decode_route(logical_agent_id, raw)
        slot = await self.repository.get_slot(logical_agent_id)
        if slot is None or slot.state == "deleted":
            raise ManagedSessionError("slot_not_found")
        repository_node = getattr(self.repository, "authority_node_id", None)
        if repository_node is not None and slot.authority_node_id != repository_node:
            raise ManagedSessionError("authority_unavailable")
        return ManagedAuthorityRoute(
            logical_agent_id=slot.logical_agent_id,
            authority_node_id=slot.authority_node_id,
            authority_epoch=slot.authority_epoch,
        )

    async def resolve(self, logical_agent_id: str) -> tuple[PersistentSlot, ManagedAuthorityRoute]:
        slot = await self.repository.get_slot(logical_agent_id)
        if slot is None or slot.state == "deleted":
            raise ManagedSessionError("slot_not_found")
        route = await self.identity_route(logical_agent_id)
        if (
            route.authority_node_id != slot.authority_node_id
            or route.authority_epoch != slot.authority_epoch
        ):
            raise ManagedSessionError("authority_unavailable")
        return slot, route


class ManagedProviderResolver:
    """Resolve trusted provider metadata to an existing LogicalAgent and route."""

    def __init__(
        self,
        repository: ManagedIdentityRepository,
        *,
        routes: ManagedFleetRoutes | None = None,
        registry: ProviderIdentityRegistry | None = None,
    ):
        self.repository = repository
        self.router = ManagedAuthorityRouter(repository, routes)
        self.registry = registry or ProviderIdentityRegistry()

    async def resolve(
        self, actor: ActorContext, provider: str, metadata: Mapping[str, object]
    ) -> ActorContext:
        if actor.provider is not None and actor.provider != provider:
            raise ManagedSessionError("identity_mismatch")
        identity = self.registry.resolve(provider, metadata)
        route_resolver = (
            getattr(self.router.routes, "resolve_provider_binding", None)
            if self.router.routes is not None
            else None
        )
        if callable(route_resolver):
            try:
                logical_agent_id = await route_resolver(identity.provider, identity.binding_key)
            except Exception as exc:
                code = getattr(exc, "code", "authority_unavailable")
                raise ManagedSessionError(str(code)) from exc
        else:
            logical_agent_id = await self.repository.resolve_provider(identity)
        if logical_agent_id is None:
            raise ManagedSessionError("identity_not_bound")
        if actor.logical_agent_id is not None and actor.logical_agent_id != logical_agent_id:
            raise ManagedSessionError("identity_mismatch")
        touch = getattr(self.repository, "touch_provider", None)
        if callable(touch):
            await touch(identity)
        route = await self.router.identity_route(logical_agent_id)
        if (
            actor.authority_node_id is not None
            and actor.authority_node_id != route.authority_node_id
        ):
            raise ManagedSessionError("authority_unavailable")
        return actor.with_agent(logical_agent_id, route.authority_node_id)

    async def bind_existing(
        self,
        actor: ActorContext,
        provider: str,
        metadata: Mapping[str, object],
        logical_agent_id: str,
        *,
        access_code: str | None = None,
    ) -> ActorContext:
        """Bind provider evidence after a separate compatibility proof of slot access.

        The caller must independently authenticate and prove access to the target
        LogicalAgent (for example by resolving an existing persistent access code).
        Provider metadata selects no permissions and cannot choose another slot.
        """
        admission = actor.admission()
        if admission is None or not actor.principal_id:
            raise ManagedSessionError("persistent_auth_required")
        admission.require("terminal:execute")
        if actor.provider is not None and actor.provider != provider:
            raise ManagedSessionError("identity_mismatch")
        identity = self.registry.resolve(provider, metadata)
        if actor.logical_agent_id is not None and actor.logical_agent_id != logical_agent_id:
            raise ManagedSessionError("identity_mismatch")
        route = await self.router.identity_route(logical_agent_id)
        repository_node = getattr(self.repository, "authority_node_id", None)
        route_binder = (
            getattr(self.router.routes, "bind_provider_binding", None)
            if self.router.routes is not None
            else None
        )
        if callable(route_binder):
            if not access_code:
                raise ManagedSessionError("access_code_required")
            try:
                bound = await route_binder(
                    identity.provider,
                    identity.binding_key,
                    logical_agent_id,
                    access_code=access_code,
                    principal_id=actor.principal_id,
                )
            except Exception as exc:
                code = getattr(exc, "code", "authority_unavailable")
                raise ManagedSessionError(str(code)) from exc
            if bound != logical_agent_id:
                raise ManagedSessionError("identity_binding_conflict")
            if repository_node == route.authority_node_id:
                await self.repository.bind_provider(
                    identity, logical_agent_id, principal_id=actor.principal_id
                )
        else:
            if repository_node is not None and route.authority_node_id != repository_node:
                raise ManagedSessionError("authority_unavailable")
            await self.repository.bind_provider(
                identity, logical_agent_id, principal_id=actor.principal_id
            )
        return actor.with_agent(logical_agent_id, route.authority_node_id)


class ManagedGrantAuthorizer:
    """Authorize one managed operation using existing AuthFoundation grants."""

    def __init__(
        self,
        repository: ManagedIdentityRepository,
        authority: ManagedAccessAuthority,
        *,
        routes: ManagedFleetRoutes | None = None,
    ):
        self.repository = repository
        self.authority = authority
        self.router = ManagedAuthorityRouter(repository, routes)

    @staticmethod
    def _client_ids(actor: ActorContext) -> frozenset[str]:
        credential = (actor.credential_id or "").strip()
        if not credential:
            return frozenset()
        candidates = {credential}
        if actor.auth_mode == "oauth" and credential.startswith("oauth:"):
            client_id = credential.removeprefix("oauth:")
            if client_id:
                candidates.add(client_id)
        return frozenset(candidates)

    @staticmethod
    def _grant_matches(
        grant: Mapping[str, object],
        *,
        logical_agent_id: str,
        authority_node_id: str,
        required_scope: str,
        client_ids: frozenset[str],
        operator: bool,
    ) -> bool:
        resources = grant.get("resources")
        scopes = grant.get("scopes")
        if not isinstance(resources, list) or not isinstance(scopes, list):
            return False
        if grant.get("client_id") not in client_ids:
            return False
        allowed_resources = {
            logical_agent_id,
            f"slot:{logical_agent_id}",
            f"node:{authority_node_id}",
        }
        if not any(isinstance(value, str) and value in allowed_resources for value in resources):
            return False
        if required_scope not in {value for value in scopes if isinstance(value, str)}:
            return False
        if operator and grant.get("role") != "operator":
            return False
        return True

    async def ensure_agent_grant(self, actor: ActorContext, logical_agent_id: str) -> None:
        """Grant the authenticated OAuth client access to a freshly bootstrapped agent."""
        if actor.auth_mode != "oauth" or not actor.principal_id or not actor.credential_id:
            raise ManagedSessionError("persistent_auth_required")
        credential = actor.credential_id
        client_id = (
            credential.removeprefix("oauth:") if credential.startswith("oauth:") else credential
        )
        scopes = [value for value in ("terminal:read", "terminal:execute") if value in actor.scopes]
        if "terminal:execute" not in scopes:
            raise ManagedSessionError("access_denied")
        grants = await self.authority.active_grants(actor.principal_id)
        for grant in grants:
            if not isinstance(grant, Mapping) or grant.get("client_id") != client_id:
                continue
            resources = grant.get("resources")
            granted_scopes = grant.get("scopes")
            if (
                isinstance(resources, list)
                and logical_agent_id in resources
                and isinstance(granted_scopes, list)
                and all(scope in granted_scopes for scope in scopes)
            ):
                return
        creator = getattr(self.authority, "create_grant", None)
        if not callable(creator):
            raise ManagedSessionError("authority_unavailable")
        try:
            await creator(
                actor.principal_id,
                client_id,
                [logical_agent_id],
                scopes,
                role="agent",
            )
        except Exception as exc:
            code = getattr(exc, "code", "access_denied")
            raise ManagedSessionError(str(code)) from exc

    async def authorize(
        self, actor: ActorContext, logical_agent_id: str, operation: ManagedOperation
    ) -> ManagedSlotGrant:
        if not isinstance(operation, ManagedOperation):
            raise ManagedSessionError("operation_not_allowed")
        admission = actor.admission()
        if admission is None or not actor.principal_id:
            raise ManagedSessionError("persistent_auth_required")
        client_ids = self._client_ids(actor)
        if not client_ids:
            raise ManagedSessionError("persistent_auth_required")
        slot, route = await self.router.resolve(logical_agent_id)
        if actor.auth_generation != slot.auth_generation:
            raise ManagedSessionError("auth_generation_mismatch", return_to_chat=True)
        route_access = (
            getattr(self.router.routes, "get_access_slot", None)
            if self.router.routes is not None
            else None
        )
        access = (
            await route_access(logical_agent_id)
            if callable(route_access)
            else await self.authority.access_slot(logical_agent_id)
        )
        if (
            not isinstance(access, Mapping)
            or access.get("logical_agent_id") != logical_agent_id
            or access.get("status") != "active"
            or access.get("authority_node_id") != route.authority_node_id
            or not isinstance(access.get("public_name"), str)
            or not str(access["public_name"]).strip()
        ):
            raise ManagedSessionError("access_slot_unavailable")
        required_scope = "terminal:read" if operation in READ_OPERATIONS else "terminal:execute"
        operator = operation in OPERATOR_OPERATIONS
        grants = await self.authority.active_grants(actor.principal_id)
        match = next(
            (
                grant
                for grant in grants
                if isinstance(grant, Mapping)
                and self._grant_matches(
                    grant,
                    logical_agent_id=logical_agent_id,
                    authority_node_id=route.authority_node_id,
                    required_scope=required_scope,
                    client_ids=client_ids,
                    operator=operator,
                )
            ),
            None,
        )
        if match is None:
            raise ManagedSessionError("access_denied")
        return ManagedSlotGrant(
            logical_agent_id=logical_agent_id,
            authority_node_id=route.authority_node_id,
            authority_epoch=route.authority_epoch,
            public_name=str(access["public_name"]),
            principal_id=actor.principal_id,
            auth_generation=slot.auth_generation,
            operator=operator,
        )
