"""Construct trusted application actors from server-owned transport state."""

from collections.abc import Mapping

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.api import TerminalApplication
from terminal_mcp.core.persistent_admission import current_admission_context
from terminal_mcp.trace import current_trace_id


def actor_for(
    service,
    *,
    transport: str,
    endpoint_role: str = "legacy",
    contract_version: int = 1,
    peer_node_id: str | None = None,
    provider: str | None = None,
    provider_metadata: Mapping[str, object] | None = None,
    request_id: str | None = None,
) -> ActorContext:
    if isinstance(service, TerminalApplication):
        service = service.service
    backend = getattr(service, "persistent", None)
    lifecycle = getattr(backend, "lifecycle", None)
    node_id = getattr(lifecycle, "authority_node_id", "")
    return ActorContext.from_admission(
        current_admission_context(),
        node_id=str(node_id or ""),
        transport=transport,
        endpoint_role=endpoint_role,
        contract_version=contract_version,
        peer_node_id=peer_node_id,
        provider=provider,
        provider_metadata=provider_metadata or {},
        request_id=request_id or current_trace_id.get(),
    )
