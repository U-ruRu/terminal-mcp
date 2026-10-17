from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.types import RequestParams

from terminal_mcp.adapters.mcp_identity import current_provider_evidence


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


@pytest.mark.parametrize(
    "fields",
    [
        {"threadId": "thread-a"},
    ],
)
def test_codex_thread_identity_without_required_root_or_call(fields):
    meta = RequestParams.Meta.model_validate(fields)
    token = request_ctx.set(SimpleNamespace(meta=meta))
    try:
        evidence = current_provider_evidence()
    finally:
        request_ctx.reset(token)
    assert evidence.provider == "codex"
    assert evidence.metadata == {"threadId": "thread-a"}
