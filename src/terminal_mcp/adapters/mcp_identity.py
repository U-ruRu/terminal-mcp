"""MCP transport hook for provider identity evidence.

OpenAI's ChatGPT connector supplies conversation identity in MCP request ``_meta``.
Those opaque values identify the caller conversation; they are deliberately kept
separate from OAuth/admission authorization and never appear in public tool args.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mcp.server.lowlevel.server import request_ctx

_OPENAI_META_KEYS = (
    "openai/subject",
    "openai/session",
    "openai/organization",
)
_CODEX_IDENTITY_KEY = "threadId"


@dataclass(frozen=True, slots=True)
class ProviderRequestEvidence:
    provider: str
    metadata: Mapping[str, object]


def current_mcp_request_id() -> str | None:
    """Return the server-owned JSON-RPC request id for replay correlation."""
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    value = context.request_id
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value)
    return None


def provider_evidence_from_meta(raw: Mapping[str, object]) -> ProviderRequestEvidence | None:
    """Classify known identity fields; this is evidence, not client authentication.

    ChatGPT fields win when both profiles appear to preserve existing bindings.
    A callId, sessionId, or progressToken alone cannot identify an agent.
    """
    if not isinstance(raw, Mapping):
        return None
    openai = {key: raw[key] for key in _OPENAI_META_KEYS if key in raw}
    if openai:
        return ProviderRequestEvidence("openai", openai)
    if _CODEX_IDENTITY_KEY in raw:
        return ProviderRequestEvidence("codex", {_CODEX_IDENTITY_KEY: raw[_CODEX_IDENTITY_KEY]})
    return None


def current_provider_evidence() -> ProviderRequestEvidence | None:
    """Capture request-scoped MCP identity evidence without granting access."""
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    meta = context.meta
    if meta is None:
        return None
    raw = meta.model_dump(exclude_none=True) if hasattr(meta, "model_dump") else dict(meta)
    return provider_evidence_from_meta(raw)
