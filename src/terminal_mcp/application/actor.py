"""Server-resolved identity carried across every application capability.

This module has no HTTP/MCP/provider SDK dependency. Transport adapters construct
actors from verified admission data, never from public request payloads. The
binding is a compatibility bridge for existing domain services and is scoped to
one async task; it cannot leak an actor into another request.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)


@dataclass(frozen=True, slots=True)
class ActorContext:
    principal_id: str | None = None
    credential_id: str | None = None
    scopes: frozenset[str] = frozenset()
    auth_generation: int = 0
    auth_mode: str = "none"
    provider: str | None = None
    provider_metadata: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)
    transport: str = "internal"
    request_id: str | None = None
    node_id: str = ""
    endpoint_role: str = "legacy"
    contract_version: int = 1
    logical_agent_id: str | None = None
    work_session_id: str | None = None
    session_epoch: int | None = None
    authority_node_id: str | None = None
    peer_node_id: str | None = None

    def __post_init__(self) -> None:
        # Copy any caller-owned mutable scope collection; frozen must be deep
        # enough for the security-relevant attributes, not merely cosmetic.
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        if not isinstance(self.provider_metadata, Mapping):
            raise ValueError("provider_metadata must be a mapping")
        provider_metadata = dict(self.provider_metadata)
        if provider_metadata and self.provider is None:
            raise ValueError("provider required for provider_metadata")
        if any(not isinstance(key, str) or not key for key in provider_metadata):
            raise ValueError("provider_metadata keys must be nonempty strings")
        object.__setattr__(self, "provider_metadata", MappingProxyType(provider_metadata))
        if type(self.contract_version) is not int or self.contract_version < 1:
            raise ValueError("contract_version must be positive")
        if self.auth_mode != "none":
            self.admission()  # Reuse the established verified-identity invariant.
        # A resolved LogicalAgent exists before/after a WorkSession. Session
        # identity, when present, is still an indivisible (agent, session, epoch).
        if self.logical_agent_id is not None and not self.logical_agent_id:
            raise ValueError("logical_agent_id must be nonempty")
        session = (self.work_session_id, self.session_epoch)
        if any(value is not None for value in session) and (
            not all(value is not None for value in session) or self.logical_agent_id is None
        ):
            raise ValueError("resolved session identity must be complete")
        if self.session_epoch is not None and (
            type(self.session_epoch) is not int or self.session_epoch < 1
        ):
            raise ValueError("session_epoch must be positive")

    @classmethod
    def from_admission(
        cls, admission: VerifiedAdmissionContext | None, **metadata: object
    ) -> ActorContext:
        if admission is None:
            return cls(**metadata)
        return cls(
            principal_id=admission.principal_id,
            credential_id=admission.credential_id,
            scopes=admission.scopes,
            auth_generation=admission.auth_generation,
            auth_mode=admission.auth_mode,
            **{"transport": admission.transport, **metadata},
        )

    def admission(self) -> VerifiedAdmissionContext | None:
        if self.auth_mode == "none":
            return None
        return VerifiedAdmissionContext(
            principal_id=self.principal_id or "",
            credential_id=self.credential_id or "",
            scopes=self.scopes,
            auth_generation=self.auth_generation,
            transport=self.transport,
            auth_mode=self.auth_mode,
        )

    @contextmanager
    def bind(self) -> Iterator[ActorContext]:
        admission = self.admission()
        actor_token = _current_actor.set(self)
        token = bind_admission_context(admission)
        try:
            yield self
        finally:
            reset_admission_context(token)
            _current_actor.reset(actor_token)

    def with_identity(self, identity: Mapping[str, object]) -> ActorContext:
        """Enrich from a successful authority response, not agent arguments."""
        return replace(
            self,
            logical_agent_id=str(identity["logical_agent_id"]),
            work_session_id=str(identity["work_session_id"]),
            session_epoch=int(identity["session_epoch"]),
            authority_node_id=(
                str(identity["authority_node_id"])
                if identity.get("authority_node_id") is not None
                else self.authority_node_id
            ),
        )

    def with_agent(self, logical_agent_id: str, authority_node_id: str) -> ActorContext:
        """Attach a server-resolved agent without inventing an active session."""
        return replace(
            self,
            logical_agent_id=logical_agent_id,
            authority_node_id=authority_node_id,
            work_session_id=None,
            session_epoch=None,
        )


_current_actor: ContextVar[ActorContext | None] = ContextVar("terminal_mcp_actor", default=None)


def current_actor() -> ActorContext | None:
    """Server-owned caller context for nested legacy execution adapters."""
    return _current_actor.get()
