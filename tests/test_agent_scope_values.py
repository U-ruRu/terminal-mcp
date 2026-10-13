"""Hierarchical work scopes and public overlap projection."""

import pytest

from terminal_mcp.core.orchestration import find_scope_overlaps, scopes_overlap


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("repo:src/terminal_mcp", "repo:src/terminal_mcp/storage", True),
        ("repo:storage", "repo:storage", True),
        ("repo:storage", "repo:mcp", False),
    ],
)
def test_hierarchical_scope_matching(left, right, expected):
    assert scopes_overlap(left, right) is expected


def test_scope_overlaps_projects_public_name():
    sessions = [{"agent_id": "Bravo-1234", "work_scope": ["repo:src/terminal_mcp/storage"]}]
    assert find_scope_overlaps(["repo:src/terminal_mcp"], sessions) == [
        {"name": "Bravo", "scope": "repo:src/terminal_mcp/storage"}
    ]
