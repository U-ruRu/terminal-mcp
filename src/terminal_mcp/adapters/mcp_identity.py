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


@dataclass(frozen=True, slots=True)
class ProviderRequestEvidence:
    provider: str
    metadata: Mapping[str, object]


def current_provider_evidence() -> ProviderRequestEvidence | None:
    """Capture provider identity from server-side MCP request context.

    The MCP client cannot promote this evidence into authorization: admission,
    AuthFoundation grants and authority fencing are evaluated independently.
    """
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    meta = context.meta
    if meta is None:
        return None
    raw = meta.model_dump(exclude_none=True) if hasattr(meta, "model_dump") else dict(meta)
    evidence = {key: raw[key] for key in _OPENAI_META_KEYS if key in raw}
    if not evidence:
        return None
    return ProviderRequestEvidence("openai", evidence)
