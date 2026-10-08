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


def test_role_messages_share_durable_obligations_across_metadata(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        sender = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "legacy"},
            request_id=100,
            conversation="sender-access",
        )
        receiver = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "legacy"},
            request_id=200,
            conversation="receiver-access",
        )
        for role, conversation, slot in (
            ("executor", "sender-exec", sender),
            ("executor", "receiver-exec", receiver),
            ("coordinator", "receiver-coord", receiver),
        ):
            value = call(
                client,
                role,
                "session",
                {"issuer_node_id": "firstbyte", "access_code": slot["access_code"]},
                conversation=conversation,
            )
            assert value["ok"], value
        sent = call(
            client,
            "executor",
            "message",
            {
                "action": "send",
                "text": "ack before next command",
                "target": receiver["public_name"],
                "mode": "ack",
                "scope": "local",
            },
            request_id=300,
            conversation="sender-exec",
        )
        assert sent["ok"], sent
        assert sent["message"]["state"] == "delivered" and sent["message"]["outcome"] == "committed"
        message_hash = sent["message"]["message_hash"]
        blocked = call(
            client,
            "executor",
            "command_run",
            {"command": "true"},
            request_id=301,
            conversation="receiver-exec",
        )
        assert blocked["ok"] is False, blocked
        inbox = call(
            client, "coordinator", "message", {"action": "read"}, conversation="receiver-coord"
        )
        assert any(item["message_hash"] == message_hash for item in inbox["messages"]), inbox
        ack = call(
            client,
            "coordinator",
            "message",
            {"action": "ack", "message_hash": message_hash},
            request_id=302,
            conversation="receiver-coord",
        )
        assert ack["ok"], ack
        assert call(
            client,
            "executor",
            "command_run",
            {"command": "true"},
            request_id=303,
            conversation="receiver-exec",
        )["ok"]
        alert = call(
            client,
            "executor",
            "message",
            {
                "action": "send",
                "text": "reply required",
                "target": receiver["public_name"],
                "mode": "alert",
                "scope": "local",
            },
            request_id=304,
            conversation="sender-exec",
        )
        alert_hash = alert["message"]["message_hash"]
        assert (
            call(
                client,
                "executor",
                "command_run",
                {"command": "true"},
                request_id=305,
                conversation="receiver-exec",
            )["ok"]
            is False
        )
        ack = call(
            client,
            "coordinator",
            "message",
            {"action": "ack", "message_hash": alert_hash},
            request_id=306,
            conversation="receiver-coord",
        )
        assert ack["ok"]
        assert (
            call(
                client,
                "executor",
                "command_run",
                {"command": "true"},
                request_id=307,
                conversation="receiver-exec",
            )["ok"]
            is False
        )
        reply = call(
            client,
            "coordinator",
            "message",
            {"action": "reply", "message_hash": alert_hash, "text": "resolved"},
            request_id=308,
            conversation="receiver-coord",
        )
        assert reply["ok"], reply
        assert call(
            client,
            "executor",
            "command_run",
            {"command": "true"},
            request_id=309,
            conversation="receiver-exec",
        )["ok"]
        recipients = call(
            client,
            "executor",
            "message",
            {"action": "recipients", "scope": "local"},
            conversation="sender-exec",
        )
        assert recipients["recipients"][0]["public_name"] == receiver["public_name"]
        assert recipients["recipients"][0]["last_active_at"]
        health = call(client, "coordinator", "health", {}, meta=False)
        assert health["status"] == "healthy", health


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


def test_executor_checkpoint_shares_coordinator_history_and_preserves_state(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        slot = call(client, "access", "session", {"action": "start", "mode": "legacy"})
        binding = {"issuer_node_id": "firstbyte", "access_code": slot["access_code"]}
        for role in ["executor", "coordinator"]:
            assert call(client, role, "session", binding)["ok"]
        key = {"namespace": "mesh-checkpoints", "task_id": "one"}
        created = call(
            client,
            "coordinator",
            "task_manage",
            {
                "action": "create",
                **key,
                "title": "checkpoint",
                "state": "in_progress",
                "isolation_hint": "independent temporary test database",
            },
            request_id=10,
        )
        assert created["ok"], created
        claimed = call(
            client,
            "executor",
            "task_claim",
            {"action": "claim", **key, "claim_intent": "verify checkpoint"},
            request_id=11,
        )
        assert claimed["ok"], claimed
        checkpoint = call(
            client,
            "executor",
            "task_comment",
            {
                "action": "checkpoint",
                **key,
                "checkpoint": {"step": "first"},
                "expected_revision": 1,
            },
            request_id=12,
        )
        assert checkpoint["ok"], checkpoint
        assert checkpoint["action"] == "checkpoint"
        assert checkpoint["task"]["state"] == "in_progress"
        assert checkpoint["task"]["revision"] == 2
        commented = call(
            client,
            "executor",
            "task_comment",
            {**key, "comment_text": "checkpoint saved"},
            request_id=13,
        )
        assert commented["ok"], commented
        read = call(client, "coordinator", "task_get", key)
        assert read["ok"], read
        stored = client.portal.call(
            app.state.service.task_store.get_task, key["namespace"], key["task_id"]
        )
        assert stored["checkpoint"] == {"step": "first"} and stored["state"] == "in_progress"
        malformed = call(
            client,
            "executor",
            "task_comment",
            {"action": "checkpoint", **key, "comment_text": "wrong field"},
            request_id=14,
        )
        assert malformed["error"]["code"] == "input_validation_failed", malformed
        released = call(
            client,
            "executor",
            "task_claim",
            {"action": "release", **key, "release_reason": "checkpoint handed over"},
            request_id=15,
        )
        assert released["ok"] and released["task"]["state"] == "in_progress", released
        after_release = client.portal.call(
            app.state.service.task_store.get_task, key["namespace"], key["task_id"]
        )
        assert released["task"]["revision"] == after_release["revision"]
        assert after_release["checkpoint"] == stored["checkpoint"]
        unowned = call(
            client,
            "executor",
            "task_state",
            {
                **key,
                "state": "blocked",
                "blocker_reason": "fixture blocker",
                "expected_revision": released["task"]["revision"],
            },
            request_id=160,
        )
        assert unowned["error"]["code"] == "owner_required"
        reclaimed = call(
            client,
            "executor",
            "task_claim",
            {**key, "action": "claim", "claim_intent": "complete verified fixture"},
            request_id=161,
        )
        assert reclaimed["ok"], reclaimed
        blocked = call(
            client,
            "executor",
            "task_state",
            {
                **key,
                "state": "blocked",
                "blocker_reason": "fixture blocker",
                "expected_revision": released["task"]["revision"],
            },
            request_id=16,
        )
        assert blocked["ok"] and blocked["task"]["state"] == "blocked", blocked
        reclaimed = call(
            client,
            "executor",
            "task_claim",
            {**key, "action": "claim", "claim_intent": "resolve verified fixture"},
            request_id=162,
        )
        assert reclaimed["ok"], reclaimed
        done = call(
            client,
            "coordinator",
            "task_manage",
            {
                **key,
                "action": "done",
                "result": {"verified": True},
                "expected_revision": blocked["task"]["revision"],
            },
            request_id=17,
        )
        assert done["ok"] and done["task"]["state"] == "done", done
        archive = call(
            client,
            "coordinator",
            "task_manage",
            {
                **key,
                "action": "archive",
                "archive_note": "verified fixture",
                "expected_revision": done["task"]["revision"],
            },
            request_id=18,
        )
        assert archive["ok"], archive
        # Replays return the immutable committed checkpoint receipt after later mutations.
        replay = call(
            client,
            "executor",
            "task_comment",
            {
                **key,
                "action": "checkpoint",
                "checkpoint": {"step": "first"},
                "expected_revision": 1,
            },
            request_id=12,
        )
        assert replay["ok"] and replay["task"] == checkpoint["task"], replay
        assert len(rpc(client, "executor", "tools/list").json()["result"]["tools"]) == 10
