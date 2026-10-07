"""Agent-facing annotations match every tool's possible side effects."""

from types import SimpleNamespace

import pytest

from terminal_mcp.mcp.roles import build_role_mcp


@pytest.mark.parametrize(
    ("role", "destructive_names", "open_world_names"),
    [
        (
            "executor",
            {
                "session",
                "message",
                "command_run",
                "command_recovery",
                "command_cancel",
                "task_state",
                "task_comment",
            },
            {"command_run", "command_recovery"},
        ),
        ("coordinator", {"session", "message", "task_manage"}, set()),
    ],
)
def test_role_annotations_describe_real_effects(role, destructive_names, open_world_names):
    mcp = build_role_mcp(SimpleNamespace(persistent=None), role)
    for tool in mcp._tool_manager.list_tools():
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is (tool.name in destructive_names), tool.name
        assert tool.annotations.openWorldHint is (tool.name in open_world_names), tool.name
        assert tool.annotations.readOnlyHint is (
            tool.name
            in {"task_list", "task_get", "task_graph", "agent_observe", "health", "command_read"}
        ), tool.name
