"""Real OAuth/pairing HTTP middleware regression, with a read-only operator spy.

Domain grant enforcement is covered independently by managed-identity tests.
This test isolates the mixed bearer/OAuth transport boundary without widening it.
"""

from fastapi.testclient import TestClient
from test_console_events import ORIGIN, pair, settings

from terminal_mcp.app import create_app
from terminal_mcp.auth.service import AuthService


def test_paired_read_scope_reaches_only_managed_status_and_revocation_still_blocks(
    tmp_path, monkeypatch
):
    config = settings(
        tmp_path,
        actions_auth_mode="bearer",
        bearer_tokens="test-console-static-token",
        persistent_agents_enabled=True,
        access_mesh_enabled=False,
    )
    app = create_app(config)
    calls = []

    async def status(actor, logical_agent_id):
        assert actor.endpoint_role == "operator"
        assert set(actor.scopes) == {"terminal:read"}
        calls.append(logical_agent_id)
        return {"ok": True, "logical_agent_id": logical_agent_id}

    monkeypatch.setattr(app.state.application.operator, "managed_status", status)
    with TestClient(app, base_url=ORIGIN) as client:
        paired = pair(client, app)
        token = AuthService(config, app.state.oauth_store).issue_access(
            paired["client_id"], "terminal:read"
        )
        headers = {"Authorization": f"Bearer {token}"}
        read = client.post(
            "/actions/persistent/managed/status",
            headers=headers,
            json={"logical_agent_id": "fixture-slot"},
        )
        assert read.status_code == 200
        assert read.json().get("ok") is True
        assert calls == ["fixture-slot"]
        # A read token must not enter any adjacent mutation or execution route.
        for path in (
            "/actions/persistent/managed/policy",
            "/actions/persistent/managed/window",
            "/actions/persistent/managed/sessions/end",
            "/actions/persistent/slots/create",
            "/actions/access/mutate",
            "/actions/agent/start",
        ):
            response = client.post(path, headers=headers, json={})
            assert response.status_code == 401
        assert calls == ["fixture-slot"]
        assert client.portal.call(app.state.pairing_store.revoke_device, paired["device_id"])
        denied = client.post(
            "/actions/persistent/managed/status",
            headers=headers,
            json={"logical_agent_id": "fixture-slot"},
        )
        assert denied.status_code == 401
        assert calls == ["fixture-slot"]
