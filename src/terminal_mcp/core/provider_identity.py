"""Provider-neutral, bounded identity evidence. This is NOT authorization.

Transport adapters choose a configured provider and pass that provider's metadata
here. Principals, OAuth scopes and authority admission remain separate mandatory
application checks. Raw provider identifiers are neither public tool arguments
nor durable task/session identities and must not be put in ordinary responses.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

PROVIDER_ID_MAX_CHARS = 2048
PROVIDER_ID_MAX_BYTES = 8192
_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class ProviderIdentityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identifier(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or not value.strip():
        raise ProviderIdentityError("identity_metadata_missing")
    if len(value) > PROVIDER_ID_MAX_CHARS:
        raise ProviderIdentityError("identity_metadata_invalid")
    try:
        valid = len(value.encode("utf-8")) <= PROVIDER_ID_MAX_BYTES
    except UnicodeEncodeError as exc:
        raise ProviderIdentityError("identity_metadata_invalid") from exc
    if not valid or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderIdentityError("identity_metadata_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider: str
    subject: str = field(repr=False)
    conversation: str = field(repr=False)
    organization: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_NAME.fullmatch(self.provider):
            raise ProviderIdentityError("identity_provider_invalid")
        _identifier(self.subject)
        _identifier(self.conversation)
        _identifier(self.organization, optional=True)

    @property
    def binding_key(self) -> str:
        """Stable across nodes, credentials, reconnects and endpoint roles.

        Optional organization metadata is not part of conversation identity: its
        absence on one otherwise-identical request cannot create a second agent.
        Providers needing an organization-scoped subject must scope that subject
        in their adapter. The authority still validates tenant access separately.
        """
        canonical = json.dumps(
            ["terminal-mcp-provider-identity-v1", self.provider, self.subject, self.conversation],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def diagnostic(self) -> dict[str, str]:
        # No credential, raw conversation/subject or optional location/user agent.
        return {"provider": self.provider, "binding_key": self.binding_key}


@runtime_checkable
class ProviderIdentityAdapter(Protocol):
    provider: str

    def resolve(self, metadata: Mapping[str, object]) -> ProviderIdentity: ...


@dataclass(frozen=True, slots=True)
class MetadataIdentityAdapter:
    """A generic adapter configured by server composition, not by tool payloads."""

    provider: str
    subject_key: str
    conversation_key: str
    organization_key: str | None = None

    def __post_init__(self) -> None:
        if not _PROVIDER_NAME.fullmatch(self.provider):
            raise ProviderIdentityError("identity_provider_invalid")
        if not self.subject_key or not self.conversation_key:
            raise ProviderIdentityError("identity_adapter_invalid")
        if self.subject_key == self.conversation_key:
            raise ProviderIdentityError("identity_adapter_invalid")

    def resolve(self, metadata: Mapping[str, object]) -> ProviderIdentity:
        if not isinstance(metadata, Mapping):
            raise ProviderIdentityError("identity_metadata_missing")
        subject = _identifier(metadata.get(self.subject_key))
        conversation = _identifier(metadata.get(self.conversation_key))
        organization = (
            _identifier(metadata.get(self.organization_key), optional=True)
            if self.organization_key is not None
            else None
        )
        assert subject is not None and conversation is not None
        return ProviderIdentity(self.provider, subject, conversation, organization)


# Official field contract: https://developers.openai.com/plugins/reference
# openai/subject is the anonymized user identifier, openai/session the conversation
# identifier. userAgent/userLocation are optional hints and NEVER identity/auth.
OPENAI_IDENTITY_ADAPTER = MetadataIdentityAdapter(
    provider="openai",
    subject_key="openai/subject",
    conversation_key="openai/session",
    organization_key="openai/organization",
)


class ProviderIdentityRegistry:
    """Explicit allowlist; no provider sniffing or identity invention on failure."""

    def __init__(self, adapters: tuple[ProviderIdentityAdapter, ...] = (OPENAI_IDENTITY_ADAPTER,)):
        configured: dict[str, ProviderIdentityAdapter] = {}
        for adapter in adapters:
            if not isinstance(adapter, ProviderIdentityAdapter):
                raise ProviderIdentityError("identity_adapter_invalid")
            provider = adapter.provider
            if not isinstance(provider, str) or not _PROVIDER_NAME.fullmatch(provider):
                raise ProviderIdentityError("identity_provider_invalid")
            if provider in configured:
                raise ProviderIdentityError("identity_provider_duplicate")
            configured[provider] = adapter
        self._adapters = MappingProxyType(configured)

    def resolve(self, provider: str, metadata: Mapping[str, object]) -> ProviderIdentity:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ProviderIdentityError("identity_provider_unsupported")
        identity = adapter.resolve(metadata)
        if not isinstance(identity, ProviderIdentity) or identity.provider != provider:
            raise ProviderIdentityError("identity_adapter_invalid")
        return identity

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
