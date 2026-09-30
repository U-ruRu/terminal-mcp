import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def _settings(tmp_path, **overrides):
    values = dict(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        bearer_tokens="console-token",
    )
    values.update(overrides)
    return Settings(**values)


def test_rollout_policy_disables_only_new_legacy_admission_when_persistent_auth_is_available(
    tmp_path,
):
    assert _settings(tmp_path, persistent_agents_enabled=False).legacy_admission_allowed() is True
    assert _settings(tmp_path, persistent_agents_enabled=True).legacy_admission_allowed() is False
    assert (
        _settings(
            tmp_path, persistent_agents_enabled=True, auth_mode="none", mcp_auth_mode="none"
        ).legacy_admission_allowed()
        is True
    )
    assert (
        _settings(
            tmp_path, persistent_agents_enabled=True, legacy_agent_admission_enabled=True
        ).legacy_admission_allowed()
        is True
    )
    assert (
        _settings(
            tmp_path, persistent_agents_enabled=False, legacy_agent_admission_enabled=False
        ).legacy_admission_allowed()
        is False
    )


def test_existing_legacy_session_can_drain_after_new_admission_is_closed(tmp_path):
    app = create_app(
        _settings(tmp_path, persistent_agents_enabled=True, legacy_agent_admission_enabled=True)
    )
    headers = {"Authorization": "Bearer console-token"}
    plan = {
        "task_summary": "Legacy drain",
        "intent": "Drain safely",
        "details": ["Drain existing session"],
        "work_scope": ["legacy"],
    }
    with TestClient(app) as client:
        proposed = client.post("/actions/agent/start", json=plan, headers=headers).json()
        started = client.post(
            "/actions/agent/start",
            json={**plan, "agent_id": proposed["proposed_agent_id"]},
            headers=headers,
        ).json()
        agent_id = started["self"]["agent_id"]
        app.state.service.legacy_agent_admission_enabled = False
        blocked = asyncio.run(app.state.service.agent_start(**plan))
        assert blocked["code"] == "legacy_admission_disabled"
        coordinated = client.post(
            "/actions/coordinate",
            json={"agent_id": agent_id, "step": 1, "intent": "Finish drain"},
            headers=headers,
        ).json()
        assert coordinated["ok"] is True
        finished = client.post(
            "/actions/agent/finish", json={"agent_id": agent_id}, headers=headers
        ).json()
        assert finished["ok"] is True


def test_console_snapshot_projects_persistent_slot_policy_and_audit(tmp_path):
    app = create_app(
        _settings(
            tmp_path,
            persistent_agents_enabled=True,
            mcp_auth_mode="oauth",
            actions_auth_mode="bearer",
        )
    )
    headers = {"Authorization": "Bearer console-token"}
    with TestClient(app) as client:
        created = client.post(
            "/actions/persistent/slots/create",
            json={"display_name": "Persistent Alpha"},
            headers=headers,
        )
        assert created.status_code == 200
        assert created.json()["ok"] is True
        response = client.get("/actions/console/snapshot", headers=headers)
        assert response.status_code == 200
        persistent = response.json()["persistent"]
        assert persistent["enabled"] is True
        assert persistent["available"] is True
        assert persistent["policy"] == {
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "manual_rearm": True,
            "admission_mode": "oauth",
            "legacy_admission_enabled": False,
        }
        assert len(persistent["slots"]) == 1
        slot = persistent["slots"][0]
        assert slot["slot"]["display_name"] == "Persistent Alpha"
        assert len(slot["selector"]["selector"]) == 4
        assert slot["claims"] == []
        assert slot["attachments"] == []
        assert slot["audit"][0]["event_type"] == "create"
        assert slot["audit"][0]["principal_id"]


def test_installer_persists_persistent_rollout_defaults_without_overwriting_operator_keys():
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "install.sh").read_text()
    env_example = (root / ".env.example").read_text()
    expected = {
        "TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED": "true",
        "TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC": "1380",
        "TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC": "1200",
        "TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC": "1320",
    }
    for key, value in expected.items():
        assert f'{key}="${{{key}:-{value}}}"' in script
        assert f'ensure_env {key} "${{{key}:-{value}}}"' in script
        assert f'{key}="{value}"' in env_example

    legacy_lines = [
        line
        for line in script.splitlines()
        if "TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED" in line
    ]
    assert legacy_lines
    assert all(":-false" not in line for line in legacy_lines)
    assert any(
        "ensure_env TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED" in line for line in legacy_lines
    )
    assert '# TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED="false"' in env_example
    assert not any(
        line.startswith("TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED=")
        for line in env_example.splitlines()
    )


def test_explicit_legacy_close_reports_actual_persistent_capability(tmp_path):
    app = create_app(
        _settings(
            tmp_path,
            persistent_agents_enabled=False,
            legacy_agent_admission_enabled=False,
        )
    )
    result = asyncio.run(
        app.state.service.agent_start(
            task_summary="Legacy blocked",
            intent="Verify capability report",
            details=["No start"],
            work_scope=["legacy"],
        )
    )
    assert result["code"] == "legacy_admission_disabled"
    assert result["persistent_agents_enabled"] is False
