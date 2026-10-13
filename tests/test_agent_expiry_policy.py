"""Session expiry uses the authoritative global deadline instead of a local duration."""

from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import session_expiry_reason, utc_now, utc_text


@pytest.mark.parametrize(
    "elapsed,idle_ttl,expected", [(0, 300, None), (301, 1000, "max_session_duration")]
)
def test_global_expiry_is_authoritative_when_local_max_differs(elapsed, idle_ttl, expected):
    now = utc_now()
    session = {
        "state": "active",
        "registered_at": utc_text(now - timedelta(seconds=1200)),
        "last_activity_at": utc_text(now - timedelta(seconds=1)),
        "global_expires_at": utc_text(now + timedelta(seconds=300)),
    }
    assert (
        session_expiry_reason(
            session,
            now=now + timedelta(seconds=elapsed),
            idle_ttl_seconds=idle_ttl,
            max_session_seconds=600,
        )
        == expected
    )
