from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_policy import PersistentPolicyController


class _Persistent:
    async def slot_list(self):
        return {
            "ok": True,
            "slots": [
                {
                    "slot": {
                        "logical_agent_id": "la_active",
                        "display_name": "Active slot",
                        "state": "active",
                    }
                }
            ],
        }


class _Service:
    def __init__(self):
        self.persistent = _Persistent()
        self.legacy_agent_admission_enabled = True


class _Lifecycle:
    def __init__(self):
        self.session_duration_seconds = 1380
        self.rearm_delay_seconds = 180

    @asynccontextmanager
    async def policy_guard(self):
        yield


@pytest.mark.asyncio
async def test_timing_policy_update_is_not_blocked_by_active_slot(tmp_path: Path):
    env_path = tmp_path / "terminal-mcp.env"
    settings = SimpleNamespace(
        persistent_session_duration_sec=1380,
        persistent_session_warning_after_sec=1200,
        persistent_session_alert_after_sec=1320,
        persistent_session_rearm_after_sec=180,
        legacy_agent_admission_enabled=True,
        env_file_path=env_path,
    )
    service = _Service()
    lifecycle = _Lifecycle()
    controller = PersistentPolicyController(settings, service, lifecycle)

    result = await controller.update(
        duration_seconds=1500,
        warning_after_seconds=1200,
        alert_after_seconds=1400,
        rearm_after_seconds=300,
        legacy_admission_enabled=False,
    )

    assert result == {
        "duration_seconds": 1500,
        "warning_after_seconds": 1200,
        "alert_after_seconds": 1400,
        "rearm_after_seconds": 300,
        "legacy_admission_enabled": False,
    }
    assert lifecycle.session_duration_seconds == 1500
    assert lifecycle.rearm_delay_seconds == 300
    assert service.legacy_agent_admission_enabled is False
    content = env_path.read_text()
    assert 'TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC="1500"' in content
    assert 'TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC="300"' in content
