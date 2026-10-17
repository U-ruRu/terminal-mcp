from __future__ import annotations

import pytest

from terminal_mcp.core.provider_identity import (
    ProviderIdentity,
    ProviderIdentityError,
    ProviderIdentityRegistry,
)


def metadata(**extra):
    return {"openai/subject": "opaque-subject", "openai/session": "opaque-conversation", **extra}


@pytest.mark.parametrize(
    "extras", [{"node_id": "secondary"}, {"credential_id": "renewed-credential"}]
)
def test_openai_conversation_binding_is_stable_across_transport_context(extras):
    registry = ProviderIdentityRegistry()
    base = registry.resolve("openai", metadata())

    assert registry.resolve("openai", metadata(**extras)).binding_key == base.binding_key


@pytest.mark.parametrize(
    "left,right",
    [
        (
            ProviderIdentity("openai", "user", "conversation-1"),
            ProviderIdentity("openai", "user", "conversation-2"),
        ),
        (
            ProviderIdentity("openai", "user-1", "chat"),
            ProviderIdentity("openai", "user-2", "chat"),
        ),
        (
            ProviderIdentity("openai", "user", "chat"),
            ProviderIdentity("test-provider", "user", "chat"),
        ),
        (ProviderIdentity("openai", "ab", "c"), ProviderIdentity("openai", "a", "bc")),
    ],
)
def test_different_identities_cannot_alias_through_concatenation_or_normalization(left, right):
    assert left.binding_key != right.binding_key
    assert len(left.binding_key) == 64


@pytest.mark.parametrize("provider", ["openai/forged"])
def test_provider_selection_is_an_explicit_server_allowlist(provider):
    with pytest.raises(ProviderIdentityError, match="identity_provider_unsupported"):
        ProviderIdentityRegistry().resolve(provider, metadata())


@pytest.mark.parametrize("field", ["openai/subject", "openai/session"])
@pytest.mark.parametrize("value", [False], ids=["wrong-type"])
def test_identifiers_do_not_coerce_agent_controlled_values(field, value):
    with pytest.raises(ProviderIdentityError, match="identity_metadata_missing"):
        ProviderIdentityRegistry().resolve("openai", metadata(**{field: value}))


def test_raw_provider_identity_is_not_in_repr_or_diagnostics():
    identity = ProviderIdentity(
        "openai", "raw-secret-subject", "raw-secret-conversation", "raw-org"
    )
    diagnostic = identity.diagnostic()
    assert diagnostic == {"provider": "openai", "binding_key": identity.binding_key}
    assert "raw-secret" not in repr(identity)
    assert "raw-org" not in repr(identity)


@pytest.mark.parametrize(
    "principal,thread", [("different-client", "thread-a"), ("verified-client", "thread-b")]
)
def test_codex_binding_separates_principals_and_threads(principal, thread):
    registry = ProviderIdentityRegistry()
    base = registry.resolve("codex", {"threadId": "thread-a"}, principal_id="verified-client")

    changed = registry.resolve("codex", {"threadId": thread}, principal_id=principal)

    assert changed.binding_key != base.binding_key


@pytest.mark.parametrize("payload", [{}, {"threadId": []}])
def test_codex_requires_a_valid_agent_thread(payload):
    with pytest.raises(ProviderIdentityError, match="identity_metadata_missing"):
        ProviderIdentityRegistry().resolve("codex", payload, principal_id="verified")


def test_codex_requires_verified_principal():
    with pytest.raises(ProviderIdentityError, match="persistent_auth_required"):
        ProviderIdentityRegistry().resolve("codex", {"threadId": "thread", "subject": "forged"})


def test_codex_resolves_verified_principal_and_thread():
    identity = ProviderIdentityRegistry().resolve(
        "codex", {"threadId": "thread", "subject": "forged"}, principal_id="verified"
    )

    assert identity == ProviderIdentity("codex", "verified", "thread")
