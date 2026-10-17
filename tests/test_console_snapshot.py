import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.config import Settings
from terminal_mcp.core.orchestration import utc_text


def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        fleet_instance_id="test-node",
        fleet_signing_private_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        fleet_peers_json="[]",
        access_mesh_proof_key="mesh-runtime-fixture-key-never-production",
        access_mesh_peers_json="[]",
        fleet_v1_projection_enabled=False,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        actions_auth_mode="bearer",
        bearer_tokens="console-token",
    )


def test_console_snapshot_captures_cursor_before_read_models(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        before = client.portal.call(app.state.service.event_store.high_water_seq)

        async def capture_snapshot():
            return await app.state.service.console_snapshot(
                "bearer",
                public_base_url="https://terminal.example",
            )

        snapshot = client.portal.call(capture_snapshot)
        assert snapshot["ok"] is True
        assert snapshot["high_water_seq"] == before

        async def read_replay():
            return await app.state.service.event_store.read(
                since=snapshot["high_water_seq"], limit=100
            )

        replay = client.portal.call(read_replay)
        assert any(event["event_type"] == "health.changed" for event in replay["events"])
        assert replay["high_water_seq"] > snapshot["high_water_seq"]


def test_authorized_console_snapshot_has_expected_read_models(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        result = client.get(
            "/actions/console/snapshot",
            headers={"Authorization": "Bearer console-token"},
        )
    assert result.status_code == 200
    assert {"instance", "agents", "tasks", "contexts", "communications", "consistency"} <= set(
        result.json()
    )


def test_console_agents_use_current_native_sessions_and_explicit_archive(tmp_path):
    app = create_app(settings(tmp_path))
    headers = {"Authorization": "Bearer console-token"}
    actor = ActorContext(
        principal_id="operator",
        credential_id="console-token",
        auth_mode="bearer",
        auth_generation=1,
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        provider="openai",
        provider_metadata={
            "openai/session": "native-console-test",
            "openai/subject": "fixture-operator",
        },
        transport="mcp",
        endpoint_role="executor",
    )
    with TestClient(app) as client:

        async def attach_native():
            mesh = app.state.access_mesh
            access_actor = replace(actor, endpoint_role="access")
            issued = await mesh.issuer_session(access_actor, action="start")
            attached = await mesh.attach(actor, session_number=issued["session_number"])
            assert attached["ok"]
            await mesh.update_intent(actor, "Review the current native agent view")
            slot = mesh.store.attached_slot(mesh.connection_key(actor))

            def archive():
                with mesh.store._connect() as db:
                    db.execute(
                        "INSERT INTO agent_sessions(agent_id,registered_at,last_activity_at,"
                        "task_summary,intent,work_scope,state) "
                        "VALUES(?,?,?,'Archived','Old','[]', 'forced')",
                        ("old-session-history", utc_text(), utc_text()),
                    )

            await asyncio.to_thread(archive)
            return {"logical_agent_id": slot.logical_agent_id}

        native = client.portal.call(attach_native)
        overview = client.post("/actions/agents", headers=headers, json={"show_details": True})
        assert overview.status_code == 200
        body = overview.json()
        assert [row["agent_id"] for row in body["active"]] == [native["logical_agent_id"]]
        assert body["sessions"][0]["intent"] == "Review the current native agent view"
        snapshot = client.get("/actions/console/snapshot", headers=headers).json()
        assert snapshot["agents"]["sessions"][0]["agent_id"] == native["logical_agent_id"]
        assert (
            snapshot["communications"][0]["intent_journal"][0]["intent"]
            == body["sessions"][0]["intent"]
        )
        assert "persistent" not in snapshot
        history = client.post(
            "/actions/agents", headers=headers, json={"history": True, "show_details": True}
        ).json()
        assert [row["agent_id"] for row in history["sessions"]] == ["old-session-history"]
        assert history["active"] == []

        async def end_native():
            await app.state.access_mesh.issuer_session(
                replace(actor, endpoint_role="access"), action="end"
            )

        client.portal.call(end_native)
        after = client.post("/actions/agents", headers=headers, json={}).json()
        assert after["active"] == []


def test_clean_start_does_not_create_retired_registration_tables(tmp_path):
    import sqlite3

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        readiness = client.get("/health/ready")
        assert readiness.status_code == 200
        assert readiness.json()["ready"] is True
        assert readiness.json()["release"] == client.get("/health/live").json()["release"]
        with sqlite3.connect(app.state.settings.auth_database_path) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not tables.intersection(
                {
                    "auth_access_slots",
                    "auth_access_codes",
                    "auth_access_code_tombstones",
                    "auth_provider_bindings",
                }
            )
        with sqlite3.connect(app.state.settings.database_path) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not tables.intersection(
                {
                    "logical_agent_provider_bindings",
                    "logical_agent_session_policies",
                    "logical_agent_work_windows",
                    "logical_agent_session_bindings",
                }
            )
            db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=1")
        blocked = client.get("/health/ready")
        assert blocked.status_code == 503
        assert blocked.json()["reason"] == "mesh_registry_migrating"
        assert client.get("/health/live").status_code == 200
