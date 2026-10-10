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
                schema = tool["inputSchema"]
                assert schema["type"] == "object"
                if role in {"executor", "coordinator"} and tool["name"] == "session":
                    assert schema["required"] == ["session_number"]
                    assert schema["properties"]["action"]["const"] == "attach"
                elif role == "executor" and tool["name"] == "task_state":
                    # The public state mutation has precisely three required inputs.
                    assert set(schema["required"]) == {"namespace", "task_id", "state"}
                    assert set(schema["properties"]) == {"namespace", "task_id", "state"}
                else:
                    assert "required" not in schema
            public = client.get(f"/.well-known/oauth-protected-resource/terminal-mcp/{role}/v1/mcp")
            assert public.status_code == 200
            assert public.json()["resource"].endswith(f"/terminal-mcp/{role}/v1/mcp")
        missing = call(client, "executor", "session", {"session_number": "1234"})
        assert missing["error"]["code"] in {"invalid_session_number", "access_mesh_slot_not_found"}
        rejected = call(client, "executor", "session", {"action": "start"})
        assert rejected["error"]["code"] == "input_validation_failed"
        unbound = call(client, "coordinator", "agent_observe", {}, meta=False)
        assert unbound == {"ok": True, "agents": []}
        read = call(client, "executor", "task_list", {}, meta=False)
        assert read["ok"] is True
        assert call(client, "executor", "command_read", {}, meta=False)["commands"] == []


def test_issuer_replay_sealed_receipt_and_attached_shell_reads(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(
            client, "access", "session", {"action": "start"}, request_id=42
        )
        assert issued["ok"] is True, issued
        record = app.state.access_mesh.store.numbers.winner(issued["session_number"])
        assert record["issuer_id"] == "firstbyte"
        replay = call(
            client, "access", "session", {"action": "start"}, request_id=42
        )
        assert replay["ok"] is False
        assert replay["error"]["code"] == "session_already_started"
        binding = {"session_number": issued["session_number"]}
        executor = call(client, "executor", "session", binding)
        assert executor["ok"] is True, executor
        coord = call(client, "coordinator", "session", binding)
        assert coord["ok"] is True, coord
        assert executor == coord == {"ok": True}
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
        assert journal["commands"][0]["logical_agent_id"] == record["logical_agent_id"]
        assert journal["commands"][0]["issuer_node_id"] == "firstbyte"
        end = call(client, "access", "session", {"action": "end"}, request_id=44)
        assert end["ok"] is True, end
        ended = call(client, "executor", "command_run", {"command": "true"}, request_id=45)
        assert ended["ok"] is False, ended
        assert ended["error"]["code"] == "session_expired"
        assert call(client, "executor", "command_read", {"cmd_hash": cmd_hash}, meta=False)["ok"]
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("select count(*) from access_mesh_slot_replicas").fetchone()[0] == 1
            receipts = db.execute("select result_json from access_mesh_issuer_receipts").fetchall()
            assert receipts and all("access_code" not in row[0] for row in receipts)
    restarted = create_app(config)
    with TestClient(restarted, base_url="https://terminal.example") as client:
        resumed = call(client, "access", "session", {"action": "start"}, request_id=42)
        # Explicit Access.start can resume the same unexpired authorization after end.
        assert resumed == {"ok": True, "session_number": issued["session_number"]}
        state = call(client, "coordinator", "agent_observe", {})
        assert all(
            set(row) == {"public_name", "last_server", "session_duration", "last_activity"}
            for row in state["agents"]
        )
        assert call(client, "executor", "command_read", {"cmd_hash": cmd_hash}, meta=False)["ok"]


def test_access_outputs_validate_as_advertised(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        catalog = rpc(client, "access", "tools/list").json()["result"]["tools"]
        validator = Draft202012Validator(catalog[0]["outputSchema"])
        for args in ({}, {"action": "start"}, {"action": "status"}):
            result = call(client, "access", "session", args)
            validator.validate(result)


def test_role_messages_share_durable_obligations_across_metadata(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        sender = call(
            client,
            "access",
            "session",
            {"action": "start"},
            request_id=100,
            conversation="sender-access",
        )
        receiver = call(
            client,
            "access",
            "session",
            {"action": "start"},
            request_id=200,
            conversation="receiver-access",
        )
        from terminal_mcp.storage.access_mesh import public_name
        recipient_slot = app.state.access_mesh.store.numbers.winner(receiver["session_number"])
        recipient_name = public_name(
            recipient_slot["issuer_id"], recipient_slot["logical_agent_id"]
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
                {"session_number": slot["session_number"]},
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
                "target": recipient_name,
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
                "target": recipient_name,
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
        assert recipients["recipients"][0]["public_name"] == recipient_name
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
        slot = call(client, "access", "session", {"action": "start"})
        logical_agent_id = app.state.access_mesh.store.numbers.winner(slot["session_number"])[
            "logical_agent_id"
        ]
        binding = {"session_number": slot["session_number"]}
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
            },
            request_id=12,
        )
        assert checkpoint["ok"], checkpoint
        assert checkpoint == {"ok": True}
        commented = call(
            client,
            "executor",
            "task_comment",
            {**key, "comment_text": "checkpoint saved"},
            request_id=13,
        )
        assert commented["ok"], commented
        second_comment = call(
            client,
            "executor",
            "task_comment",
            {**key, "comment_text": "second comment with the same transport ID"},
            request_id=13,
        )
        assert second_comment == {"ok": True}
        read = call(client, "coordinator", "task_get", key)
        assert read["ok"], read
        stored = client.portal.call(
            app.state.service.task_store.get_task, key["namespace"], key["task_id"]
        )
        assert stored["checkpoint"] == {"step": "first"} and stored["state"] == "in_progress"
        assert stored["revision"] == 1
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
            },
            request_id=160,
        )
        assert unowned == {"ok": True}
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
            },
            request_id=16,
        )
        assert blocked == {"ok": True}
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
            "executor",
            "task_state",
            {**key, "state": "done"},
            request_id=17,
        )
        assert done == {"ok": True}
        done_task = client.portal.call(
            app.state.service.task_store.get_task, key["namespace"], key["task_id"]
        )
        assert done_task["state"] == "done"
        # Task state does not increment content revision.
        assert done_task["revision"] == stored["revision"]
        archive = call(
            client,
            "coordinator",
            "task_manage",
            {
                **key,
                "action": "archive",
                "archive_note": "verified fixture",
                "expected_revision": done_task["revision"],
            },
            request_id=18,
        )
        assert archive["ok"], archive
        # The same transport ID carries a second checkpoint operation.
        repeated_id = call(
            client,
            "executor",
            "task_comment",
            {
                **key,
                "action": "checkpoint",
                "checkpoint": {"step": "first"},
            },
            request_id=12,
        )
        assert repeated_id == {"ok": True}
        events = client.portal.call(
            app.state.service.task_store.list_events, key["namespace"], key["task_id"]
        )
        checkpoints = [event for event in events if event["event_type"] == "checkpoint"]
        comments = [
            event
            for event in events
            if event["event_type"] == "comment" and event["payload"].get("kind") == "comment"
        ]
        assert len(checkpoints) == 2, events
        assert len(comments) == 2, events
        assert all(event["agent_id"] == logical_agent_id for event in checkpoints + comments)
        assert len(rpc(client, "executor", "tools/list").json()["result"]["tools"]) == 10
