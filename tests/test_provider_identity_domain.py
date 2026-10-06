from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from terminal_mcp.core.provider_identity import (
    OPENAI_IDENTITY_ADAPTER,
    PROVIDER_ID_MAX_CHARS,
    MetadataIdentityAdapter,
    ProviderIdentity,
    ProviderIdentityError,
    ProviderIdentityRegistry,
)


def metadata(**extra):
    return {"openai/subject": "opaque-subject", "openai/session": "opaque-conversation", **extra}


def test_identical_conversation_is_stable_across_credential_node_role_and_provider_hints():
    registry = ProviderIdentityRegistry()
    base = registry.resolve("openai", metadata())
    for extras in (
        {"node_id": "bacloud"},
        {"node_id": "secondary"},
        {"credential_id": "renewed-oauth-credential"},
        {"openai/organization": "optional-organization"},
        {"openai/userAgent": "different-client"},
        {"role": "executor", "contract_version": 3},
        {"role": "coordinator", "contract_version": 1},
        {"openai/userLocation": {"city": "other"}},
        {"openai/locale": "ru"},
        {"provider": "forged"},
        {"logical_agent_id": "la_forged", "work_session_id": "ws_forged"},
    ):
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
        (ProviderIdentity("openai", "a|b", "c"), ProviderIdentity("openai", "a", "b|c")),
        (ProviderIdentity("openai", "user ", "chat"), ProviderIdentity("openai", "user", "chat")),
    ],
)
def test_different_identities_cannot_alias_through_concatenation_or_normalization(left, right):
    assert left.binding_key != right.binding_key
    assert len(left.binding_key) == 64


def test_generic_adapter_does_not_require_openai_sdk_or_field_names():
    custom = MetadataIdentityAdapter("other-provider", "account", "thread", "tenant")
    registry = ProviderIdentityRegistry((OPENAI_IDENTITY_ADAPTER, custom))
    identity = registry.resolve("other-provider", {"account": "a", "thread": "t", "tenant": "org"})
    assert identity == ProviderIdentity("other-provider", "a", "t", "org")
    assert registry.providers == ("openai", "other-provider")


@pytest.mark.parametrize("provider", ["", "OpenAI", "openai/forged", "unknown"])
def test_provider_selection_is_an_explicit_server_allowlist(provider):
    with pytest.raises(ProviderIdentityError, match="identity_provider_unsupported"):
        ProviderIdentityRegistry().resolve(provider, metadata())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"openai/subject": "subject"},
        {"openai/session": "session"},
        {"subject": "subject", "conversation": "conversation"},
        {"Authorization": "Bearer token", "user-agent": "ChatGPT"},
        {"openai/userAgent": "ChatGPT", "openai/userLocation": {"country": "US"}},
        {"logical_agent_id": "agent", "sender": "name", "access_code": "1234"},
        [],
        None,
    ],
)
def test_missing_metadata_never_falls_back_to_public_identity_fields(payload):
    with pytest.raises(ProviderIdentityError, match="identity_metadata_missing"):
        ProviderIdentityRegistry().resolve("openai", payload)


@pytest.mark.parametrize("field", ["openai/subject", "openai/session", "openai/organization"])
@pytest.mark.parametrize("value", ["", "  ", False, 42, [], {}])
def test_identifiers_do_not_coerce_agent_controlled_values(field, value):
    with pytest.raises(ProviderIdentityError, match="identity_metadata_missing"):
        ProviderIdentityRegistry().resolve("openai", metadata(**{field: value}))


@pytest.mark.parametrize(
    "value",
    [
        "x" * (PROVIDER_ID_MAX_CHARS + 1),
        "line\nbreak",
        "tab\tvalue",
        "\x00null",
        "\x7f",
        "\ud800",
    ],
)
def test_invalid_or_unbounded_ids_fail_without_echoing_sensitive_input(value):
    with pytest.raises(ProviderIdentityError) as caught:
        ProviderIdentityRegistry().resolve("openai", metadata(**{"openai/subject": value}))
    assert str(caught.value) == "identity_metadata_invalid"


def test_raw_provider_identity_is_not_in_repr_or_diagnostics():
    identity = ProviderIdentity(
        "openai", "raw-secret-subject", "raw-secret-conversation", "raw-org"
    )
    diagnostic = identity.diagnostic()
    assert diagnostic == {"provider": "openai", "binding_key": identity.binding_key}
    assert "raw-secret" not in repr(identity)
    assert "raw-org" not in repr(identity)
    with pytest.raises(FrozenInstanceError):
        identity.subject = "other"


def test_duplicate_registration_and_malformed_adapter_are_rejected():
    with pytest.raises(ProviderIdentityError, match="identity_provider_duplicate"):
        ProviderIdentityRegistry((OPENAI_IDENTITY_ADAPTER, OPENAI_IDENTITY_ADAPTER))
    with pytest.raises(ProviderIdentityError, match="identity_adapter_invalid"):
        ProviderIdentityRegistry((object(),))


def test_registered_adapter_cannot_return_another_provider_namespace():
    class WrongAdapter:
        provider = "custom"

        def resolve(self, _metadata):
            return ProviderIdentity("openai", "subject", "chat")

    with pytest.raises(ProviderIdentityError, match="identity_adapter_invalid"):
        ProviderIdentityRegistry((WrongAdapter(),)).resolve("custom", {})


def test_opaque_unicode_identifier_has_deterministic_bounded_digest():
    identity = ProviderIdentity("custom", "пользователь", "диалог")
    assert identity.binding_key == ProviderIdentity("custom", "пользователь", "диалог").binding_key
    assert identity.binding_key.isascii()
