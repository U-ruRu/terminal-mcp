"""Knowledge-style MCP discovery, separate from the strict execution boundary.

Packaged metadata is compiled from the real Pydantic contracts at development
time. At server construction, check exact model hashes before publishing it.
The compatibility switch is intentionally read at startup, not per request.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from terminal_mcp.mcp.role_contracts import schema_digest

OBJECT_SCHEMA = {"type": "object"}
MODE_ENV = "TERMINAL_MCP_AGENT_SCHEMA_MODE"


@lru_cache(maxsize=1)
def bundled_catalog() -> dict:
    path = Path(__file__).with_name("agent_catalogs.json")
    return json.loads(path.read_text(encoding="utf-8"))


def install_agent_contract(
    mcp, role: str, models: dict[str, type], *, mesh: bool = True
) -> None:
    """Publish object-only schemas without replacing FastMCP's arg_model/validator.

    A stale packaged description is an installation error instead of silently
    misdocumenting runtime behavior. Set MODE_ENV=legacy to restore the prior
    discovery representation without touching code, data or in-flight sessions.
    """
    mode = os.environ.get(MODE_ENV, "knowledge")
    if mode == "legacy":
        return
    if mode != "knowledge":
        raise ValueError(f"Unknown {MODE_ENV} value; expected knowledge or legacy")

    catalog = bundled_catalog()[role if mesh or role == "access" else role + "_native"]
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    if set(tools) != set(catalog) or set(models) != set(tools):
        raise RuntimeError(f"Agent discovery catalog roster drift for {role}")

    # Validate every tool before mutating any of the published metadata.
    for name, tool in tools.items():
        item = catalog[name]
        if (tool.description or "") != item["base_description"]:
            raise RuntimeError(f"Agent discovery description drift for {role}.{name}")
        input_hash = schema_digest(models[name].model_json_schema())
        if input_hash != item["strict_input_sha256"]:
            raise RuntimeError(f"Agent discovery input contract drift for {role}.{name}")
        output_hash = schema_digest(tool.fn_metadata.output_model.model_json_schema())
        if output_hash != item["strict_output_sha256"]:
            raise RuntimeError(f"Agent discovery output contract drift for {role}.{name}")

    for name, tool in tools.items():
        tool.description = catalog[name]["description"]
        tool.parameters = OBJECT_SCHEMA.copy()
        tool.fn_metadata.output_schema = OBJECT_SCHEMA.copy()

    mcp.agent_schema_mode = "knowledge"
