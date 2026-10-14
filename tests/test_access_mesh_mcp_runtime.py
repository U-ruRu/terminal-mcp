"""Complete authenticated HTTP/MCP Access + Executor + Coordinator admission."""

import base64
import json

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "terminal.env",
        log_path=tmp_path / "terminal.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        bearer_tokens="mesh-test-token",
        mcp_auth_mode="",
        actions_auth_mode="",
        persistent_agents_enabled=True,
        fleet_instance_id="firstbyte",
        fleet_id="",
        fleet_signing_private_key=base64.b64encode(bytes(range(32))).decode(),
        fleet_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        access_mesh_enabled=True,
        access_mesh_proof_key="mesh-runtime-fixture-key-never-production",
        access_mesh_peers_json="[]",
    )


def rpc(
    client, role, method, params=None, *, request_id=1, meta=True, conversation=None, token=True
):
    headers = {"accept": "application/json, text/event-stream"}
    if token:
        headers["authorization"] = "Bearer mesh-test-token"
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    if meta:
        body["params"]["_meta"] = {
            "openai/subject": "fixture-subject",
            "openai/session": conversation or f"fixture-{role}",
        }
    return client.post(f"/terminal-mcp/{role}/v1/mcp/", json=body, headers=headers)


def call(client, role, name, arguments, **kwargs):
    # Normal runtime fixtures explicitly start work with synthetic intent. Tests
    # for the missing-intent path use the raw rpc helper instead.
    if (
        role in {"executor", "coordinator"}
        and name == "session"
        and arguments.get("action", "attach") == "attach"
        and "session_number" in arguments
    ):
        arguments = {"intent": "Exercise isolated runtime fixture", **arguments}
    response = rpc(client, role, "tools/call", {"name": name, "arguments": arguments}, **kwargs)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    result = body["result"]
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def test_access_outputs_validate_as_advertised(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        catalog = rpc(client, "access", "tools/list").json()["result"]["tools"]
        validator = Draft202012Validator(catalog[0]["outputSchema"])
        for args in ({}, {"action": "start"}, {"action": "status"}):
            result = call(client, "access", "session", args)
            validator.validate(result)


def test_message_projection_does_not_hide_queued_remote_delivery():
    from terminal_mcp.mcp.output_contracts import message_result

    raw = {
        "ok": True,
        "action": "send",
        "message_hash": "firstbyte:meshmsg:fixture",
        "sender": "firstbyte-fixture",
        "target": "bacloud-fixture",
        "scope": "fleet",
        "mode": "notify",
        "state": "queued",
        "outcome": "committed",
        "delivered_to": [],
        "pending_peers": ["bacloud"],
        "delivery_errors": [
            {"server_id": "bacloud", "code": "message_unavailable", "retry": "retry"}
        ],
    }
    result = message_result(
        raw,
        sender=raw["sender"],
        text="hello",
        target=raw["target"],
        message_hash=None,
        mode="notify",
        require_reply=False,
        alert=False,
        show_all=False,
    )
    assert result.isError is False
    assert result.structuredContent["message"]["state"] == "queued"
    assert result.structuredContent["message"]["pending_peers"] == ["bacloud"]
    assert result.structuredContent["message"]["outcome"] == "committed"
