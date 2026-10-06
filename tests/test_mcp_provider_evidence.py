from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.types import RequestParams

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.mcp.server import _trusted_mcp_provider_evidence


def test_openai_identity_evidence_is_read_only_from_transport_meta_and_allowlisted():
    meta = RequestParams.Meta.model_validate(
        {
            "openai/subject": "opaque-user",
            "openai/session": "opaque-conversation",
            "openai/organization": "opaque-org",
            "userAgent": "ignored",
            "untrusted-extra": "ignored",
        }
    )
    token = request_ctx.set(SimpleNamespace(meta=meta))
    try:
        provider, evidence = _trusted_mcp_provider_evidence()
    finally:
        request_ctx.reset(token)
    assert provider == "openai"
    assert evidence == {
        "openai/subject": "opaque-user",
        "openai/session": "opaque-conversation",
        "openai/organization": "opaque-org",
    }


def test_provider_evidence_absence_preserves_legacy_actor_path():
    token = request_ctx.set(SimpleNamespace(meta=RequestParams.Meta()))
    try:
        assert _trusted_mcp_provider_evidence() == (None, {})
    finally:
        request_ctx.reset(token)


def test_actor_copies_and_freezes_transport_provider_metadata():
    source = {"openai/subject": "user", "openai/session": "conversation"}
    actor = ActorContext(provider="openai", provider_metadata=source)
    source["openai/session"] = "tampered"
    assert actor.provider_metadata["openai/session"] == "conversation"
    with pytest.raises(TypeError):
        actor.provider_metadata["openai/session"] = "tampered"
    with pytest.raises(ValueError, match="provider required"):
        ActorContext(provider_metadata={"openai/subject": "user"})
