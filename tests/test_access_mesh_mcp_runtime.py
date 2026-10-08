"""Complete authenticated HTTP/MCP Access + Executor + Coordinator admission."""

import base64
import json
import sqlite3

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
    response = rpc(client, role, "tools/call", {"name": name, "arguments": arguments}, **kwargs)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    result = body["result"]
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def test_access_endpoints_auth_metadata_and_exact_role_catalogs(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        assert rpc(client, "access", "tools/list", token=False).status_code == 401
        for role, count in (("access", 1), ("executor", 10), ("coordinator", 8)):
            result = rpc(client, role, "tools/list")
            assert result.status_code == 200, result.text
            tools = result.json()["result"]["tools"]
            assert len(tools) == count
            for tool in tools:
                assert tool["inputSchema"]["type"] == "object"
                assert "required" not in tool["inputSchema"]
            public = client.get(f"/.well-known/oauth-protected-resource/terminal-mcp/{role}/v1/mcp")
            assert public.status_code == 200
            assert public.json()["resource"].endswith(f"/terminal-mcp/{role}/v1/mcp")
        missing = call(client, "executor", "session", {"access_code": "1234"})
        assert missing["error"]["code"] == "input_validation_failed"
        assert missing["error"]["path"] == "issuer_node_id"
        rejected = call(client, "executor", "session", {"action": "start"})
        assert rejected["error"]["code"] == "input_validation_failed"
        unbound = call(client, "coordinator", "agent_observe", {}, meta=False)
        assert unbound["attached"] is False
        read = call(client, "executor", "task_list", {}, meta=False)
        assert read["ok"] is True
        assert call(client, "executor", "command_read", {}, meta=False)["commands"] == []


def test_issuer_replay_sealed_receipt_and_attached_shell_reads(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(
            client, "access", "session", {"action": "start", "mode": "legacy"}, request_id=42
        )
        assert issued["ok"] is True, issued
        assert issued["issuer_node_id"] == "firstbyte"
        replay = call(
            client, "access", "session", {"action": "start", "mode": "legacy"}, request_id=42
        )
        assert replay == issued
        binding = {"issuer_node_id": issued["issuer_node_id"], "access_code": issued["access_code"]}
        executor = call(client, "executor", "session", binding)
        assert executor["ok"] is True, executor
        coord = call(client, "coordinator", "session", binding)
        assert coord["ok"] is True, coord
        assert (
            executor["logical_agent_id"] == coord["logical_agent_id"] == issued["logical_agent_id"]
        )
        assert executor["work_session_id"] == coord["work_session_id"]
        ran = call(
            client,
            "executor",
            "command_run",
            {"command": "printf 'mesh-runtime-ok\\n'"},
            request_id=43,
        )
        assert ran["ok"] is True, ran
        cmd_hash = ran["command"]["cmd_hash"]
        read = call(client, "executor", "command_read", {"cmd_hash": cmd_hash}, meta=False)
        assert read["ok"] is True, read
        assert "mesh-runtime-ok" in "\n".join(read["lines"])
        journal = call(client, "executor", "command_read", {}, meta=False)
        assert journal["ok"] is True, journal
        assert journal["commands"][0]["logical_agent_id"] == issued["logical_agent_id"]
        assert journal["commands"][0]["issuer_node_id"] == "firstbyte"
        end = call(client, "access", "session", {"action": "end"}, request_id=44)
        assert end["ok"] is True, end
        ended = call(client, "executor", "command_run", {"command": "true"}, request_id=45)
        assert ended["ok"] is False, ended
        assert ended["error"]["code"] == "window_cooldown"
        assert call(client, "executor", "command_read", {"cmd_hash": cmd_hash}, meta=False)["ok"]
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("select count(*) from access_mesh_slot_replicas").fetchone()[0] == 1
            receipts = db.execute("select result_json from access_mesh_issuer_receipts").fetchall()
            assert receipts and all("access_code" not in row[0] for row in receipts)
    restarted = create_app(config)
    with TestClient(restarted, base_url="https://terminal.example") as client:
        assert (
            call(client, "access", "session", {"action": "start", "mode": "legacy"}, request_id=42)
            == issued
        )
        state = call(client, "coordinator", "agent_observe", {})
        assert state["logical_agent_id"] == issued["logical_agent_id"]
        assert call(client, "executor", "command_read", {"cmd_hash": cmd_hash}, meta=False)["ok"]


def test_access_outputs_validate_as_advertised(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        catalog = rpc(client, "access", "tools/list").json()["result"]["tools"]
        validator = Draft202012Validator(catalog[0]["outputSchema"])
        for args in ({}, {"action": "start", "mode": "legacy"}, {"action": "status"}):
            result = call(client, "access", "session", args)
            validator.validate(result)
