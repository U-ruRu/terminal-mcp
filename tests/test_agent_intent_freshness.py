"""Public call-boundary intent freshness, with isolated clocks and databases."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import rpc, settings

from terminal_mcp.app import create_app
from terminal_mcp.core.managed_sessions import ManagedOperation


def invoke(client, role, name, args, request_id=1, **kwargs):
    response = rpc(
        client,
        role,
        "tools/call",
        {"name": name, "arguments": args},
        request_id=request_id,
        **kwargs,
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result


def start(client, *, initial=True):
    issued = invoke(client, "access", "session", {"action": "start"})["structuredContent"]
    args = {"session_number": issued["session_number"]}
    if initial:
        args["intent"] = "Verify isolated fixture commands"
    assert invoke(client, "executor", "session", args)["structuredContent"] == {"ok": True}
    return issued


def advance(mesh, seconds):
    now = mesh.clock() + timedelta(seconds=seconds)
    mesh.clock = lambda: now
    return now


def test_initial_intent_required_and_refresh_without_number(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        start(client, initial=False)
        rejected = invoke(client, "executor", "command_run", {"command": "true"})
        assert rejected["structuredContent"]["error"]["code"] == "intent_required"
        assert invoke(client, "executor", "command_read", {})["structuredContent"]["ok"]
        refreshed = invoke(
            client,
            "executor",
            "session",
            {"action": "intent", "intent": "Check the fixture command path"},
        )["structuredContent"]
        assert refreshed["ok"] and refreshed["intent_updated_at"]
        assert invoke(client, "executor", "command_run", {"command": "true"})["structuredContent"][
            "ok"
        ]
        assert not invoke(client, "executor", "session", {"action": "intent", "intent": "   "})[
            "structuredContent"
        ]["ok"]


def test_exact_boundary_first_write_warns_next_write_has_no_effect(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        mesh = app.state.access_mesh
        fixed = datetime.now(UTC)
        mesh.clock = lambda: fixed
        start(client)
        advance(mesh, 179.999)
        before = invoke(client, "executor", "command_read", {})
        assert "intent_warning" not in before.get("_meta", {})
        advance(mesh, 0.001)
        first = invoke(client, "executor", "command_run", {"command": "true"}, 10)
        assert first["structuredContent"]["ok"]
        assert "next write will be blocked" in first["_meta"]["intent_warning"]
        with sqlite3.connect(config.database_path) as db:
            count = db.execute("SELECT count(*) FROM commands").fetchone()[0]
        blocked = invoke(client, "executor", "command_run", {"command": "true"}, 11)
        assert blocked["structuredContent"]["error"]["code"] == "intent_stale"
        assert blocked["structuredContent"]["error"]["outcome"] == "not_committed"
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == count
        assert invoke(
            client,
            "executor",
            "session",
            {"action": "intent", "intent": "Continue fixture validation"},
        )["structuredContent"]["ok"]
        assert invoke(client, "executor", "command_run", {"command": "true"}, 12)[
            "structuredContent"
        ]["ok"]


def test_failed_read_consumes_warning_and_safe_paths_are_not_trapped(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        start(client)
        advance(app.state.access_mesh, 180)
        failed = invoke(client, "executor", "command_read", {"limit": 0})
        assert failed["structuredContent"]["error"]["code"] == "input_validation_failed"
        assert failed["_meta"]["intent_warning"]
        for tool, args in (
            ("command_read", {}),
            ("command_cancel", {"cmd_hash": "missing"}),
            ("command_recovery", {"command": "true"}),
        ):
            result = invoke(client, "executor", tool, args)["structuredContent"]
            assert result.get("error", {}).get("code") not in {"intent_stale", "intent_required"}
        assert invoke(client, "access", "session", {"action": "end"})["structuredContent"]["ok"]


def test_concurrent_observations_have_exactly_one_grace_and_restart_keeps_gate(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = start(client)
        mesh = app.state.access_mesh
        now = advance(mesh, 180)
        slot = mesh.store.code_slot(mesh.store.local_node_id, issued["session_number"])
        assert slot

        def observe(_):
            return mesh.store.intent_status(slot, now=now, observe=True, require_intent=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(observe, range(8)))
        assert sum(result["warn"] for result in statuses) == 1
        assert sum(not result["blocked"] for result in statuses) == 1
        store = create_app(config).state.access_mesh.store
        assert store.intent_status(slot, now=now, observe=True, require_intent=True)["blocked"]


def test_pagination_filter_scope_and_long_command_reads_do_not_refresh_intent(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        for index in range(3):
            issued = invoke(
                client, "access", "session", {"action": "start"}, conversation=f"access-{index}"
            )["structuredContent"]
            invoke(
                client,
                "executor",
                "session",
                {"session_number": issued["session_number"], "intent": f"Check fixture {index}"},
                conversation=f"executor-{index}",
            )
        records, cursor = [], None
        while True:
            args = {"limit": 1}
            if cursor:
                args["cursor"] = cursor
            page = invoke(client, "coordinator", "agent_observe", args)["structuredContent"]
            assert len(page["agents"]) <= 1
            records.extend(page["agents"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        assert len(records) == 3 and len({r["logical_agent_id"] for r in records}) == 3
        running = invoke(
            client, "executor", "command_run", {"command": "sleep 2"}, 88, conversation="executor-0"
        )["structuredContent"]
        command_hash = running["cmd_hash"]
        advance(app.state.access_mesh, 180)
        first = invoke(
            client,
            "executor",
            "command_read",
            {"cmd_hash": command_hash},
            conversation="executor-0",
        )
        assert first["_meta"]["intent_warning"]
        second = invoke(
            client,
            "executor",
            "command_read",
            {"cmd_hash": command_hash},
            conversation="executor-0",
        )["structuredContent"]
        assert second["command"]["cmd_hash"] == command_hash
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1
        assert (
            invoke(
                client,
                "executor",
                "command_cancel",
                {"cmd_hash": command_hash},
                conversation="executor-0",
            )["structuredContent"]
            .get("error", {})
            .get("code")
            != "intent_stale"
        )


def test_repeated_attach_preserves_intent_and_does_not_bypass_refresh(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = start(client)
        advance(app.state.access_mesh, 180)
        attached = invoke(
            client,
            "executor",
            "session",
            {"session_number": issued["session_number"], "intent": "Replacement is not a refresh"},
        )
        assert attached["structuredContent"] == {"ok": True}
        assert attached["_meta"]["intent_warning"]
        blocked = invoke(client, "executor", "command_run", {"command": "true"})
        assert blocked["structuredContent"]["error"]["code"] == "intent_stale"
        record = invoke(client, "coordinator", "agent_observe", {})["structuredContent"]["agents"][
            0
        ]
        assert record["current_intent"] == "Verify isolated fixture commands"


def test_access_restart_without_reattach_requires_new_epoch_intent(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        start(client)
        first = invoke(client, "coordinator", "agent_observe", {})["structuredContent"]["agents"][0]
        advance(app.state.access_mesh, 5)
        assert invoke(client, "access", "session", {"action": "end"})["structuredContent"]["ok"]
        advance(app.state.access_mesh, 5)
        assert invoke(client, "access", "session", {"action": "start"})["structuredContent"]["ok"]
        blocked = invoke(client, "executor", "command_run", {"command": "true"})[
            "structuredContent"
        ]
        assert blocked["error"]["code"] == "intent_required"
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
        second = invoke(client, "coordinator", "agent_observe", {})["structuredContent"]["agents"][
            0
        ]
        assert second["work_session_id"] != first["work_session_id"]
        assert second["session_epoch"] == first["session_epoch"] + 1
        assert second["current_intent"] is None
        assert invoke(
            client,
            "executor",
            "session",
            {"action": "intent", "intent": "Check the restarted fixture cycle"},
        )["structuredContent"]["ok"]
        assert invoke(client, "executor", "command_run", {"command": "true"})["structuredContent"][
            "ok"
        ]


def test_intent_refresh_materializes_authorized_restart_without_any_prior_write(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        start(client)
        advance(app.state.access_mesh, 5)
        invoke(client, "access", "session", {"action": "end"})
        advance(app.state.access_mesh, 5)
        invoke(client, "access", "session", {"action": "start"})
        assert invoke(
            client,
            "executor",
            "session",
            {"action": "intent", "intent": "Start the authorized next fixture cycle"},
        )["structuredContent"]["ok"]
        assert invoke(client, "executor", "command_run", {"command": "true"})["structuredContent"][
            "ok"
        ]


def test_grace_does_not_cross_a_concurrently_replaced_declaration(tmp_path, monkeypatch):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = start(client)
        mesh = app.state.access_mesh
        advance(mesh, 180)
        original_resolve = mesh.resolve
        changed = False

        async def interleaved_resolve(actor, operation):
            nonlocal changed
            if operation == ManagedOperation.COMMAND_RUN and not changed:
                changed = True
                slot = mesh.store.code_slot(mesh.store.local_node_id, issued["session_number"])
                identity = mesh.store.observed_identity(slot, now=mesh.clock())
                mesh.store.update_intent(
                    slot,
                    identity["work_session_id"],
                    "Concurrent new fixture declaration",
                    now=mesh.clock(),
                )
                now = advance(mesh, 180)
                assert mesh.store.intent_status(slot, now=now, observe=True)["warn"]
            return await original_resolve(actor, operation)

        monkeypatch.setattr(mesh, "resolve", interleaved_resolve)
        result = invoke(client, "executor", "command_run", {"command": "true"})["structuredContent"]
        assert result["error"]["code"] == "intent_stale"
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
