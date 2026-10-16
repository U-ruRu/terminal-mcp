"""AuthMiddleware owns interface routing and minimum scope requirements."""

import pytest

from terminal_mcp.auth.middleware import AuthMiddleware


@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/terminal-mcp/executor/v1/mcp",
        "/terminal-mcp/coordinator/v1/mcp",
    ],
)
def test_role_mcp_paths_use_mcp_auth_interface(path):
    assert AuthMiddleware._interface(path) == "mcp"


def test_neighbor_path_is_not_an_mcp_interface():
    assert AuthMiddleware._interface("/terminal-mcp/executor/v1/mcpx") is None


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


@pytest.mark.parametrize(
    "path,method,expected",
    [
        ("/actions/persistent/slots/migrate-access", "POST", ["terminal:read", "terminal:execute"]),
        (
            "/actions/persistent/slots/rotate-access-code",
            "POST",
            ["terminal:read", "terminal:execute"],
        ),
        ("/actions/persistent/slots/migrate-access", "GET", None),
    ],
)
def test_paired_console_access_code_handoff_routes_require_execute_scope(path, method, expected):
    assert AuthMiddleware._paired_console_scopes(path, method) == expected
