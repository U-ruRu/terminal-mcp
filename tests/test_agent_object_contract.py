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
    # Component validators own field-specific rejection classes. This adapter
    # check keeps one rejected call per role to pin the common MCP HTTP error
    # envelope and pre-commit result; repeating every validator case here
    # exercised the same response mapping rather than another adapter path.
    cases = [
        ("access", "session", {}, "action"),
        ("executor", "command_run", {"command": 2}, "command"),
        (
            "coordinator",
            "task_manage",
            {"action": "create", "namespace": "a"},
            "isolation_hint",
        ),
    ]
    with TestClient(app, base_url="https://terminal.example") as client:
        before = call(client, "coordinator", "task_list", {}, meta=False)
        assert before["tasks"] == []
        for i, (role, tool, args, path) in enumerate(cases, 50):
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


def test_legacy_discovery_mode_is_not_supported(tmp_path, monkeypatch):
    monkeypatch.setenv(MODE_ENV, "legacy")
    with pytest.raises(ValueError, match=MODE_ENV):
        create_app(settings(tmp_path))


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


def test_single_agent_catalog_has_no_native_or_obsolete_session_descriptions():
    catalog = bundled_catalog()
    assert set(catalog) == {"access", "executor", "coordinator"}
    for role in ("executor", "coordinator"):
        assert 'action?:"attach"|"intent"' in catalog[role]["session"]["description"]
        for entry in catalog[role].values():
            assert "session(action='start')" not in entry["description"]
        assert "Start, end, or interrupt" not in catalog[role]["session"]["description"]
    assert "logical_agent_id" in catalog["coordinator"]["agent_observe"]["description"]
