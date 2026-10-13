"""Actual ASGI ingress/auth/logging composition acceptance for Events."""

import logging

from fastapi.testclient import TestClient
from persistent_app_test_helpers import persistent_app_with_execution_port as create_app

from terminal_mcp.config import Settings


def _settings(tmp_path):
    return Settings(
        database_path=tmp_path / "web.db",
        auth_database_path=tmp_path / "auth.db",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "runtime.log",
        metrics_port=0,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=False,
        legacy_agent_admission_enabled=True,
        cwd=tmp_path,
        public_base_url="https://example.test",
        auth_mode="bearer",
        bearer_tokens="safe-test-credential",
        mcp_auth_mode="",
        actions_auth_mode="",
    )


def test_actual_asgi_subscribe_no_auth_reports_stage_and_no_secrets(tmp_path, caplog):
    (tmp_path / "runtime.env").write_text("TERMINAL_MCP_DETAILED_LOGGING=true\n")
    app = create_app(_settings(tmp_path))
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    private = "whsec_EXAMPLE_PRIVATE_MATERIAL_NEVER_LOG"
    req = {
        "jsonrpc": "2.0",
        "id": 52,
        "method": "events/subscribe",
        "params": {
            "name": "agent.attached",
            "arguments": {},
            "delivery": {
                "mode": "webhook",
                "url": "https://private.example.test/secret-path",
                "secret": private,
            },
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/terminal-mcp/coordinator/v1/mcp/", json=req, headers={"accept": "application/json"}
        )
        assert response.status_code in (401, 403)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "tmcp_events_diag" in logs
    assert "events/subscribe" in logs
    assert '"stage":"ingress"' in logs
    assert '"stage":"http_response"' in logs
    assert "http_401" in logs or "http_403" in logs
    assert private not in logs
    assert "private.example.test" not in logs
    assert "/secret-path" not in logs
