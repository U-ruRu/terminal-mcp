"""AuthMiddleware owns interface routing and minimum scope requirements."""

import pytest

from terminal_mcp.auth.middleware import AuthMiddleware


@pytest.mark.parametrize(
    "path",
    [
        "/terminal-mcp/access/v1/mcp",
        "/terminal-mcp/executor/v1/mcp",
        "/terminal-mcp/coordinator/v1/mcp",
    ],
)
def test_role_mcp_paths_use_mcp_auth_interface(path):
    assert AuthMiddleware._interface(path) == "mcp"


@pytest.mark.parametrize(
    "path",
    [
        "/actions/run",
    ],
)
def test_agent_facing_oauth_uses_one_read_scope(path):
    assert AuthMiddleware._scopes(path, "POST") == ["terminal:read"]


def test_paired_console_fleet_enrollment_requires_execute_scope():
    from terminal_mcp.auth.middleware import AuthMiddleware

    assert AuthMiddleware._paired_console_scopes("/actions/fleet/control/enrollment", "GET") == [
        "terminal:read",
        "terminal:execute",
    ]


def test_paired_console_slot_mutation_requires_execute_scope():
    assert AuthMiddleware._paired_console_scopes(
        "/actions/access/mutate", "POST"
    ) == ["terminal:read", "terminal:execute"]
