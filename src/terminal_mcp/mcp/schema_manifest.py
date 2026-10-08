"""Reproducible manifests of the effective published and strict wire contracts."""

from terminal_mcp.mcp.role_contracts import schema_digest, serialized_schema


def mesh_schema_manifest(servers: dict) -> dict:
    roles = {}
    for role, server in sorted(servers.items()):
        contracts = {item["tool_name"]: item for item in server.role_schema_contract["tools"]}
        tools = []
        for tool in server._tool_manager.list_tools():
            runtime = tool.fn_metadata.output_model.model_json_schema()
            tools.append(
                {
                    "name": tool.name,
                    "runtime_input_digest": contracts[tool.name]["runtime_input_schema_digest"],
                    "planning_input_digest": schema_digest(tool.parameters),
                    "planning_input_bytes": len(serialized_schema(tool.parameters)),
                    "runtime_output_digest": schema_digest(runtime),
                    "planning_output_digest": schema_digest(tool.output_schema),
                    "planning_output_bytes": len(serialized_schema(tool.output_schema)),
                    "annotations": tool.annotations.model_dump(mode="json", exclude_none=True),
                }
            )
        roles[role] = {"endpoint": f"/terminal-mcp/{role}/v1/mcp", "tools": tools}
    return {"contract_version": 1, "access_mesh": True, "roles": roles}
