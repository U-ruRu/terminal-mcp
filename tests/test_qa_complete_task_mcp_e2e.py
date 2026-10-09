"""End-to-end MCP/HTTP acceptance for the nine-point public Task contract.

The real FastAPI/MCP adapters and SQLite implementations run against a
disposable QA database, not a user's active session or production tasks.
"""

import asyncio

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, rpc, settings

from terminal_mcp.app import create_app
from terminal_mcp.storage.tasks import TaskStore


def test_access_attach_task_api_six_states_cas_append_and_self_send(tmp_path):
    app = create_app(settings(tmp_path))
    namespace = "qa-tmcp-acceptance"
    with TestClient(app, base_url="https://terminal.example") as client:
        started = call(client, "access", "session", {"action": "start"}, request_id=100)
        assert set(started) == {"ok", "session_number"} and started["ok"]
        number = started["session_number"]
        for role in ("coordinator", "executor"):
            assert call(client, role, "session", {"session_number": number}, request_id=101) == {
                "ok": True
            }

        create_payload = {
            "action": "create",
            "namespace": namespace,
            "title": "Full QA public task contract",
            "description": "Exactly one task from same author, title and description",
            "isolation_hint": "none",
        }
        first = call(client, "coordinator", "task_manage", create_payload, request_id=110)
        assert first["ok"] and set(first) == {"ok", "task_id"}, first
        task_id = first["task_id"]
        duplicated = call(client, "coordinator", "task_manage", create_payload, request_id=111)
        assert not duplicated["ok"] and duplicated["error"]["code"] == "duplicate_task"
        key = {"namespace": namespace, "task_id": task_id}

        baseline = asyncio.run(TaskStore(settings(tmp_path).database_path).get_task(**key))
        assert baseline["revision"] == 1

        for step, state in enumerate(
            ("in_progress", "qa", "blocked", "deferred", "done", "ready"), start=120
        ):
            response = rpc(
                client,
                "executor",
                "tools/call",
                {"name": "task_state", "arguments": {**key, "state": state}},
                request_id=step,
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["structuredContent"] == {"ok": True}, result
            assert "remaining_time" in result["_meta"]
            assert (
                await_result := asyncio.run(
                    TaskStore(settings(tmp_path).database_path).get_task(**key)
                )
            )["state"] == state
            assert await_result["revision"] == baseline["revision"]

        updated = call(
            client,
            "coordinator",
            "task_manage",
            {"action": "update", **key, "title": "Updated QA task", "expected_revision": 1},
            request_id=130,
        )
        assert updated == {"ok": True, "revision": 2}, updated
        repeated = call(
            client,
            "coordinator",
            "task_manage",
            {"action": "update", **key, "title": "Updated QA task", "expected_revision": 1},
            request_id=131,
        )
        assert not repeated["ok"] and repeated["error"]["code"] == "already_changed"
        assert (
            asyncio.run(TaskStore(settings(tmp_path).database_path).get_task(**key))["state"]
            == "ready"
        )

        added = call(
            client,
            "executor",
            "task_comment",
            {"action": "comment", **key, "comment_text": "QA append exact"},
            request_id=140,
        )
        assert added == {"ok": True}, added
        same = call(
            client,
            "executor",
            "task_comment",
            {"action": "comment", **key, "comment_text": "QA append exact"},
            request_id=141,
        )
        assert not same["ok"] and same["error"]["code"] == "duplicate_comment"
        for step in range(2):
            checkpoint = call(
                client,
                "executor",
                "task_comment",
                {"action": "checkpoint", **key, "checkpoint": {"step": step}},
                request_id=150 + step,
            )
            assert checkpoint == {"ok": True}, checkpoint

        active = call(client, "coordinator", "agent_observe", {}, request_id=160)
        assert active["ok"] and len(active["agents"]) == 1
        name = active["agents"][0]["public_name"]
        assert set(active["agents"][0]) == {
            "public_name",
            "last_server",
            "session_duration",
            "last_activity",
        }
        self_send = call(
            client,
            "executor",
            "message",
            {"action": "send", "target": name, "text": "QA self-send", "scope": "local"},
            request_id=161,
        )
        assert not self_send["ok"] and self_send["error"]["code"] == "cannot_message_self"

        finished = call(client, "access", "session", {"action": "end"}, request_id=170)
        assert finished == {"ok": True}
        after_end = call(client, "executor", "task_state", {**key, "state": "qa"}, request_id=171)
        assert not after_end["ok"], after_end
