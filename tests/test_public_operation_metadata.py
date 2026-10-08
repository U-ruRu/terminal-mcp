import os
from types import SimpleNamespace

import pytest

from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.operation_metadata import (
    MUTATING_ACTIONS,
    READ_ONLY_ACTIONS,
    action_is_consequential,
)


def test_legacy_metadata_describes_aggregate_tool_side_effects():
    server = build_mcp(SimpleNamespace(persistent=None))
    for tool in server._tool_manager.list_tools():
        readonly = tool.name in {"observe", "health"}
        assert tool.annotations.readOnlyHint is readonly
        assert tool.annotations.destructiveHint is (not readonly)
        assert tool.annotations.openWorldHint is (tool.name == "cmd")
        assert tool.annotations.idempotentHint is readonly


@pytest.mark.parametrize("persistent", [False, True])
def test_openapi_metadata_matches_all_registered_operations(tmp_path, monkeypatch, persistent):
    for key in list(os.environ):
        if key.startswith("TERMINAL_MCP_"):
            monkeypatch.delenv(key)
    from terminal_mcp.app import create_app
    from terminal_mcp.config import Settings

    app = create_app(
        Settings(
            _env_file=None,
            database_path=tmp_path / "state.sqlite3",
            persistent_agents_enabled=persistent,
        )
    )
    schema = app.openapi()
    seen = set()
    for path in schema["paths"].values():
        for operation in path.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            name = operation["operationId"]
            seen.add(name)
            assert operation["x-openai-isConsequential"] is (name in MUTATING_ACTIONS)
            assert operation["security"] == [{"BearerAuth": []}]
    assert seen <= READ_ONLY_ACTIONS | MUTATING_ACTIONS
    if persistent:
        assert {"getPersistentSlot", "createPersistentSlot", "getManagedWorkSessionStatus"} <= seen
    assert {"getConsoleSnapshot", "runCommand", "readTerminal", "mutateTask"} <= seen
    assert action_is_consequential("futureUnreviewedMutation") is True
