import asyncio

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "db.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=False,
        legacy_agent_admission_enabled=True,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        actions_auth_mode="bearer",
        bearer_tokens="console-token",
    )


def test_console_snapshot_is_authenticated_and_returns_complete_read_model(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/actions/console/snapshot").status_code == 401
        headers = {"Authorization": "Bearer console-token"}

        plan = {
            "task_summary": "Snapshot agent",
            "intent": "Exercise console snapshot",
            "details": ["Create state", "Inspect snapshot"],
            "work_scope": ["snapshot-test"],
        }
        proposed = client.post(
            "/actions/agent/start",
            json=plan,
            headers=headers,
        )
        assert proposed.status_code == 200
        started = client.post(
            "/actions/agent/start",
            json={"agent_id": proposed.json()["proposed_agent_id"], **plan},
            headers=headers,
        )
        assert started.status_code == 200
        agent_id = started.json()["self"]["agent_id"]

        created = client.post(
            "/actions/context",
            json={
                "action": "create",
                "summary": "Snapshot context",
                "content": "Visible authenticated context.",
                "primary": True,
            },
            headers=headers,
        )
        assert created.status_code == 200

        task = client.post(
            "/actions/task",
            json={
                "agent_id": agent_id,
                "action": "create",
                "namespace": "snapshot",
                "task_id": "DONE",
                "title": "Completed task",
                "lane": "implementation",
                "priority": "P2",
                "state": "done",
                "result": {"ok": True},
                "isolation_hint": "none",
            },
            headers=headers,
        )
        assert task.status_code == 200

        active_task = client.post(
            "/actions/task",
            json={
                "agent_id": agent_id,
                "action": "create",
                "namespace": "snapshot",
                "task_id": "ACTIVE",
                "title": "Active task",
                "lane": "implementation",
                "priority": "P1",
                "state": "ready",
                "isolation_hint": "none",
            },
            headers=headers,
        )
        assert active_task.status_code == 200
        claimed = client.post(
            "/actions/task",
            json={
                "agent_id": agent_id,
                "action": "claim",
                "namespace": "snapshot",
                "task_id": "ACTIVE",
                "claim_intent": "Expose stable Console identity",
            },
            headers=headers,
        )
        assert claimed.status_code == 200

        snapshot = client.get("/actions/console/snapshot", headers=headers)
        assert snapshot.status_code == 200
        body = snapshot.json()

        assert body["ok"] is True
        assert body["high_water_seq"] >= 1
        assert body["consistency"] == {
            "mode": "cursor_first_at_least_once",
            "high_water_seq": body["high_water_seq"],
            "replay_from_seq": body["high_water_seq"],
            "duplicate_events_possible": True,
        }
        assert body["instance"]["application"] == "terminal-mcp"
        assert body["instance"]["public_base_url"] == "https://terminal.example"
        assert body["instance"]["health"]["ok"] is True
        resources = body["instance"]["resources"]
        assert resources["status"] in {"available", "partial", "unavailable"}
        assert set(resources) == {"status", "cpu", "memory", "filesystem", "uptime"}
        assert all(
            resources[key]["status"] in {"available", "unavailable"}
            for key in ("cpu", "memory", "filesystem", "uptime")
        )
        agent_name = started.json()["self"]["name"]
        agent_session = next(
            item for item in body["agents"]["sessions"] if item["name"] == agent_name
        )
        assert agent_session["agent_id"] == agent_id
        assert any(
            item["namespace"] == "snapshot" and item["task_id"] == "DONE"
            for item in body["tasks"]["tasks"]
        )
        assert body["tasks"]["summary"]["by_state"]["done"] >= 1
        active_projection = next(
            item for item in body["tasks"]["tasks"]
            if item["namespace"] == "snapshot" and item["task_id"] == "ACTIVE"
        )
        assert active_projection["owner"]["agent_id"] == agent_id
        assert active_projection["owner"]["agent_name"] == agent_name
        assert body["contexts"]["primary"] == [
            {
                "id": created.json()["entry"]["id"],
                "summary": "Snapshot context",
                "content": "Visible authenticated context.",
            }
        ]
        communication = next(
            item for item in body["communications"] if item["name"] == agent_name
        )
        assert communication["agent_id"] == agent_id
        assert "intent_journal" in communication
        assert "message_journal" in communication

        schema = client.get("/openapi.json").json()
        operation = schema["paths"]["/actions/console/snapshot"]["get"]
        assert operation["operationId"] == "getConsoleSnapshot"
        assert operation["security"] == [{"BearerAuth": []}]


def test_console_snapshot_cursor_first_contract_replays_concurrent_changes(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app):
        before = asyncio.run(app.state.service.event_store.high_water_seq())
        assert before == 0

        snapshot = asyncio.run(
            app.state.service.console_snapshot(
                "bearer",
                public_base_url="https://terminal.example",
            )
        )
        assert snapshot["ok"] is True
        assert snapshot["high_water_seq"] == before

        replay = asyncio.run(
            app.state.service.event_store.read(since=snapshot["high_water_seq"], limit=100)
        )
        assert replay["gap"] is False
        assert any(event["event_type"] == "health.changed" for event in replay["events"])
        assert replay["high_water_seq"] > snapshot["high_water_seq"]
