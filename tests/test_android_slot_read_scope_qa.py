"""Independent least-privilege contract checks for Android managed-slot reads.

The operator-read domain path requires terminal:read. This test intentionally
exposes the pre-existing paired-console fallback mismatch; do not weaken writes.
"""

import pytest

from terminal_mcp.auth.middleware import AuthMiddleware


def test_managed_status_paired_fallback_accepts_read_scope_only():
    assert AuthMiddleware._paired_console_scopes("/actions/persistent/managed/status", "POST") == [
        "terminal:read"
    ]


@pytest.mark.parametrize("action", ["policy", "window", "sessions/end"])
def test_managed_mutations_still_require_execute_scope(action):
    assert AuthMiddleware._paired_console_scopes(
        "/actions/persistent/managed/" + action, "POST"
    ) == ["terminal:read", "terminal:execute"]
