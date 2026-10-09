"""Dedicated issuer MCP. It manages issuance; execution roles attach separately."""

from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.adapters.mcp_identity import current_mcp_request_id, current_provider_evidence
from terminal_mcp.mcp.access_contracts import IssuerOutput, IssuerSessionInput
from terminal_mcp.mcp.role_contracts import (
    RuntimeBoundary,
    install_role_input_contract,
    validate_boundary,
)
from terminal_mcp.mcp.role_outputs import install_role_output_contract


def build_access_mcp(application, *, public_base_url: str = "http://127.0.0.1:8080"):
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
        description="Start an Access session and obtain its four-digit session_number, or end the current session.",
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

    install_role_input_contract(
        mcp, "access", overrides={("access", "session"): IssuerSessionInput}
    )
    install_role_output_contract(mcp, "access", overrides={("access", "session"): IssuerOutput}, application=application)
    from terminal_mcp.operation_metadata import install_action_metadata

    install_action_metadata(mcp, "access", mesh=True)
    from terminal_mcp.mcp.role_contracts import schema_digest, serialized_schema

    tool = mcp._tool_manager.get_tool("session")
    mcp.role_schema_contract = {
        "endpoint_role": "access",
        "contract_version": 1,
        "access_mesh": True,
        "tools": [
            {
                "tool_name": "session",
                "runtime_input_schema_digest": schema_digest(
                    IssuerSessionInput.model_json_schema()
                ),
                "planning_input_schema_digest": schema_digest(tool.parameters),
                "planning_input_schema_bytes": len(serialized_schema(tool.parameters)),
            }
        ],
    }
    return mcp
