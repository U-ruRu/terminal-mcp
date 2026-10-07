from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.types import RequestParams

from terminal_mcp.adapters.mcp_identity import current_provider_evidence
from terminal_mcp.application.actor import ActorContext


def test_chatgpt_provider_identity_is_captured_from_mcp_request_meta_only():
    meta = RequestParams.Meta.model_validate(
        {
            "openai/subject": "opaque-user",
            "openai/session": "opaque-conversation",
            "openai/organization": "opaque-org",
            "userAgent": "not-identity",
            "userLocation": {"city": "ignored"},
        }
    )
    token = request_ctx.set(SimpleNamespace(meta=meta))
    try:
        evidence = current_provider_evidence()
    finally:
        request_ctx.reset(token)
    assert evidence.provider == "openai"
    assert evidence.metadata == {
        "openai/subject": "opaque-user",
        "openai/session": "opaque-conversation",
        "openai/organization": "opaque-org",
    }


def test_non_provider_meta_keeps_legacy_compatibility_path():
    token = request_ctx.set(SimpleNamespace(meta=RequestParams.Meta()))
    try:
        assert current_provider_evidence() is None
    finally:
        request_ctx.reset(token)


def test_actor_provider_evidence_is_request_scoped_immutable_and_not_authorization():
    source = {"openai/subject": "u", "openai/session": "c"}
    actor = ActorContext(provider="openai", provider_metadata=source)
    source["openai/session"] = "other"
    assert actor.provider_metadata["openai/session"] == "c"
    assert actor.admission() is None
    with pytest.raises(TypeError):
        actor.provider_metadata["openai/session"] = "other"
