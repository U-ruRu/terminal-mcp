"""Actual MCP transport acceptance of the universal agent object contract.

All tests use a private sqlite fixture; no production service or deployment.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from test_access_mesh_mcp_runtime import call, rpc, settings

from terminal_mcp.app import create_app
from terminal_mcp.mcp.agent_contracts import MODE_ENV, bundled_catalog
from terminal_mcp.mcp.roles import build_role_mcp


def test_complete_http_discovery_preserves_annotations_and_contracts(tmp_path):
    app = create_app(settings(tmp_path))
    expected = {"access": 2, "executor": 10, "coordinator": 8}
    with TestClient(app, base_url="https://terminal.example") as client:
        assert rpc(client, "access", "tools/list", token=False).status_code == 401
        for role, count in expected.items():
            tools = rpc(client, role, "tools/list").json()["result"]["tools"]
            assert len(tools) == count
            for item in tools:
                assert item["inputSchema"] == {"type": "object"}
                assert item["outputSchema"] == {"type": "object"}
                assert item["description"] == bundled_catalog()[role][item["name"]]["description"]
                for part in ("Arguments", "Returns", "Errors"):
                    assert part in item["description"]
                Draft202012Validator.check_schema(item["inputSchema"])
                assert item["annotations"]
            descriptions = {item["name"]: item["description"] for item in tools}
            if role == "access":
                assert "event_id and cursor are mutually exclusive" in descriptions["get_events"]
            else:
                assert "action=intent requires nonblank intent" in descriptions["session"]
                assert "forbids session_number" in descriptions["session"]


def test_http_validation_and_admission_are_structured_pre_commit(tmp_path):
    app = create_app(settings(tmp_path))
    cases = [
        ("access", "session", {}, "action", "required"),
        ("access", "get_events", {"limit": 21}, "limit", "le=20"),
        ("access", "get_events", {"show_read": "yes"}, "show_read", "boolean"),
        (
            "access",
            "get_events",
            {"event_id": "evt_" + "a" * 32, "cursor": "opaque"},
            "cursor",
            "omit cursor or event_id",
        ),
        ("executor", "session", {"action": "attach"}, "session_number", "four-digit string"),
        ("executor", "session", {"action": "intent"}, "intent", "nonblank string"),
        (
            "executor",
            "session",
            {"action": "intent", "intent": "refresh", "session_number": "1234"},
            "session_number",
            "omit for action=intent",
        ),
        ("executor", "command_run", {"command": 2}, "command", "string"),
        ("executor", "command_run", {}, "command", "required"),
        ("executor", "task_list", {"limit": 0}, "limit", "ge=1"),
        ("executor", "task_list", {"tags": [""]}, "tags.0", None),
        (
            "coordinator",
            "task_manage",
            {"action": "create", "namespace": "a"},
            "isolation_hint",
            None,
        ),
        ("coordinator", "message", {"action": "reply", "text": "reply"}, "message_hash", None),
    ]
    with TestClient(app, base_url="https://terminal.example") as client:
        before = call(client, "coordinator", "task_list", {}, meta=False)
        assert before["tasks"] == []
        for i, (role, tool, args, path, expected) in enumerate(cases, 50):
            response = rpc(
                client, role, "tools/call", {"name": tool, "arguments": args}, request_id=i
            )
            assert response.status_code == 200, response.text
            payload = response.json()["result"]
            assert payload["isError"] is False
            data = payload["structuredContent"]
            assert data["ok"] is False, data
            assert data["error"]["code"] == "input_validation_failed", data
            assert data["error"]["reason"] and data["error"]["path"] == path, data
            if expected is not None:
                assert data["error"]["expected"] == expected, data
            assert data["error"]["outcome"] == data["outcome"] == "not_committed"
            assert json.loads(payload["content"][0]["text"]) == data
        rejected = rpc(
            client,
            "coordinator",
            "tools/call",
            {"name": "task_list", "arguments": {"tags": [None] * 100_001}},
            request_id=81,
        )
        assert rejected.status_code == 200, rejected.text[:300]
        data = rejected.json()["result"]["structuredContent"]
        assert data["error"]["code"] == "input_admission_failed"
        assert data["error"]["reason"] == "too_many_elements"
        assert data["error"]["expected"] == "elements<=100000"
        assert data["outcome"] == "not_committed"
        after = call(client, "coordinator", "task_list", {}, meta=False)
        assert after["tasks"] == []


def test_compatibility_switch_restores_old_discovery_without_changing_execution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(MODE_ENV, "legacy")
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        for role in ("access", "executor", "coordinator"):
            tools = rpc(client, role, "tools/list").json()["result"]["tools"]
            assert any(item["inputSchema"] != {"type": "object"} for item in tools)
        error = call(client, "executor", "command_run", {"command": 2})
        assert error["error"]["code"] == "input_validation_failed"
        assert error["outcome"] == "not_committed"
        assert call(client, "executor", "task_list", {}, meta=False)["ok"] is True


def test_stale_catalog_or_unknown_mode_fails_closed(monkeypatch):
    mesh = SimpleNamespace(persistent=None, access_mesh=object())
    catalog = copy.deepcopy(bundled_catalog())
    catalog["executor"]["task_list"]["strict_input_sha256"] = "stale-model"
    monkeypatch.setattr("terminal_mcp.mcp.agent_contracts.bundled_catalog", lambda: catalog)
    with pytest.raises(RuntimeError, match="input contract drift"):
        build_role_mcp(mesh, "executor")
    monkeypatch.setenv(MODE_ENV, "unexpected")
    with pytest.raises(ValueError, match=MODE_ENV):
        build_role_mcp(mesh, "executor")


def test_native_roles_also_publish_object_only_schema():
    service = SimpleNamespace(persistent=None)
    for role, count in (("executor", 10), ("coordinator", 8)):
        server = build_role_mcp(service, role)
        assert len(server._tool_manager.list_tools()) == count
        for tool in server._tool_manager.list_tools():
            assert tool.parameters == {"type": "object"}
            assert tool.output_schema == {"type": "object"}
            assert tool.description == bundled_catalog()[role + "_native"][tool.name]["description"]
