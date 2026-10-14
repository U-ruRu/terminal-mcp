"""Dedicated issuer MCP. It manages issuance; execution roles attach separately."""

import asyncio
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.adapters.mcp_identity import current_mcp_request_id, current_provider_evidence
from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.mcp.access_contracts import (
    AccessGetEventsOutput,
    GetEventsInput,
    IssuerOutput,
    IssuerSessionInput,
)
from terminal_mcp.mcp.role_contracts import (
    RuntimeBoundary,
    install_role_input_contract,
    validate_boundary,
)
from terminal_mcp.mcp.role_outputs import install_role_output_contract


def build_access_mcp(
    application, *, event_store=None, public_base_url: str = "http://127.0.0.1:8080"
):
    parsed = urlparse(public_base_url)
    mcp = FastMCP(
        "terminal-mcp-access-v1",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", parsed.netloc],
            allowed_origins=[public_base_url],
        ),
    )

    @mcp.tool(
        name="session",
        structured_output=False,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
        ),
        description=(
            "Start an Access session and obtain its four-digit session_number, "
            "or end the current session."
        ),
    )
    async def session(*, boundary: RuntimeBoundary) -> dict:
        request, error = validate_boundary(boundary, IssuerSessionInput)
        if error is not None:
            return error
        evidence = current_provider_evidence()
        actor = actor_for(
            application,
            transport="mcp",
            endpoint_role="access",
            contract_version=1,
            provider=evidence.provider if evidence else None,
            provider_metadata=evidence.metadata if evidence else {},
            request_id=current_mcp_request_id(),
        )
        with actor.bind():
            return await application.service.access_mesh.issuer_session(
                actor, action=request.action
            )

    @mcp.tool(
        name="get_events",
        structured_output=False,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Read this agent's unconsumed MCP Events, oldest first, and mark returned "
            "events read. With event_id, return one owned event even if read; "
            "show_read=true includes previously read events. No agent ID or "
            "subscription ID is needed: identity comes from the Access binding."
        ),
    )
    async def get_events(*, boundary: RuntimeBoundary) -> dict:
        request, error = validate_boundary(boundary, GetEventsInput)
        if error is not None:
            return error
        evidence = current_provider_evidence()
        actor = actor_for(
            application,
            transport="mcp",
            endpoint_role="access",
            contract_version=1,
            provider=evidence.provider if evidence else None,
            provider_metadata=evidence.metadata if evidence else {},
            request_id=current_mcp_request_id(),
        )
        with actor.bind():
            if "terminal:read" not in actor.scopes:
                raise AccessMeshError("persistent_scope_required")
            mesh = application.service.access_mesh
            if mesh is None or event_store is None:
                raise AccessMeshError("access_mesh_unavailable")
            key = mesh.connection_key(actor)
            slot = await asyncio.to_thread(mesh.store.issuer_bound_slot, key)
            if slot is None or slot.state != "active":
                raise AccessMeshError("subscriber_identity_missing")
            return await event_store.get_events(
                slot.logical_agent_id,
                principal_id=actor.principal_id,
                event_id=request.event_id,
                limit=request.limit,
                cursor=request.cursor,
                show_read=request.show_read,
            )

    install_role_input_contract(
        mcp, "access", overrides={
            ("access", "session"): IssuerSessionInput,
            ("access", "get_events"): GetEventsInput,
        }
    )
    install_role_output_contract(
        mcp, "access", overrides={
            ("access", "session"): IssuerOutput,
            ("access", "get_events"): AccessGetEventsOutput,
        }, application=application
    )
    from terminal_mcp.operation_metadata import install_action_metadata

    install_action_metadata(mcp, "access", mesh=True)
    from terminal_mcp.mcp.role_contracts import schema_digest, serialized_schema

    mcp.role_schema_contract = {
        "endpoint_role": "access",
        "contract_version": 1,
        "access_mesh": True,
        "tools": [
            {
                "tool_name": name,
                "runtime_input_schema_digest": schema_digest(model.model_json_schema()),
                "planning_input_schema_digest": schema_digest(
                    mcp._tool_manager.get_tool(name).parameters
                ),
                "planning_input_schema_bytes": len(
                    serialized_schema(mcp._tool_manager.get_tool(name).parameters)
                ),
            }
            for name, model in (
                ("session", IssuerSessionInput),
                ("get_events", GetEventsInput),
            )
        ],
    }
    return mcp
