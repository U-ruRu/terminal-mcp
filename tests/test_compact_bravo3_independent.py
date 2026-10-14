"""Bravo-3 independent acceptance: raw source model + catalog + isolated MCP wire."""

from __future__ import annotations

import asyncio
import json
import pathlib

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.mcp.access_contracts import (
    AttachInput,
    GetEventsInput,
    IssuerSessionInput,
    MeshAgentObserveInput,
    MeshCommandReadInput,
    MeshMessageInput,
    MeshTaskCommentInput,
)
from terminal_mcp.mcp.role_contracts import (
    ROLE_TOOL_MODELS,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / "docs/compact-contracts/generated"
SHA = "d1caea24e2d8c2dff650568fac616bcee0efb99b"
ROLES = ("access", "executor", "coordinator")


def load(name):
    return json.loads((GEN / name).read_text())


def model_of(role, name):
    if role == "access":
        return {"session": IssuerSessionInput, "get_events": GetEventsInput}[name]
    override = {
        "session": AttachInput,
        "agent_observe": MeshAgentObserveInput,
        "message": MeshMessageInput,
        "command_read": MeshCommandReadInput,
        "task_comment": MeshTaskCommentInput,
    }
    if name in override and (role != "coordinator" or name not in {"command_read", "task_comment"}):
        return override[name]
    return ROLE_TOOL_MODELS[role, name]


CATALOGS = {role: load(f"catalog-{role}.json")["tools"] for role in ROLES}
SIDECAR = load("strict-contracts.json")

# Evaluated by hand against access_contracts.py, role_contracts.py,
# task_requests.py, roles.py, access.py and the generated descriptions.
CASES = {
    "access.session": ({"action": "start"}, {"action": "other"}),
    "access.get_events": ({}, {"limit": 21}),
    "executor.session": ({"session_number": "0010"}, {"session_number": "1"}),
    "executor.task_list": ({}, {"limit": 0}),
    "executor.command_run": ({"command": "printf qa"}, {"command": 12}),
    "executor.command_read": ({}, {"limit": 101}),
    "executor.command_cancel": ({"cmd_hash": "h"}, {"cmd_hash": ""}),
    "executor.command_recovery": ({"command": "printf qa"}, {"command": ""}),
    "executor.task_claim": (
        {"action": "release", "namespace": "qa", "task_id": "t"},
        {"action": "claim", "namespace": "qa", "task_id": "t", "claim_intent": 1},
    ),
    "executor.task_state": (
        {"namespace": "qa", "task_id": "t", "state": "qa"},
        {"namespace": "qa", "task_id": "t", "state": "bad"},
    ),
    "executor.task_comment": (
        {"namespace": "qa", "task_id": "t", "comment_text": "ok"},
        {"action": "checkpoint", "namespace": "qa", "task_id": "t", "checkpoint": None},
    ),
    "executor.message": ({"action": "read"}, {"action": "ack", "message_hash": None}),
    "coordinator.session": ({"session_number": "0010"}, {"session_number": "x"}),
    "coordinator.task_get": (
        {"namespace": "qa", "task_id": "t"},
        {"namespace": "qa", "task_id": "t", "detail": "unknown"},
    ),
    "coordinator.task_list": ({}, {"tags": [""]}),
    "coordinator.task_manage": (
        {"action": "create", "namespace": "qa", "isolation_hint": "isolated"},
        {"action": "update", "namespace": "qa", "task_id": "t", "title": "change"},
    ),
    "coordinator.task_graph": (
        {"namespace": "qa", "task_id": "t"},
        {"namespace": "qa", "task_id": "t", "direction": "sideways"},
    ),
    "coordinator.agent_observe": ({}, {"unexpected": True}),
    "coordinator.message": ({"action": "read"}, {"action": "ack", "message_hash": None}),
    "coordinator.health": ({}, {"extended": "yes"}),
}
assert len(CASES) == 20


def make_settings(path):
    import base64

    return Settings(
        database_path=path / "db.sqlite3",
        auth_database_path=path / "auth.sqlite3",
        output_cache_path=path / "output.sqlite3",
        runtime_config_path=path / "runtime.env",
        env_file_path=path / "terminal.env",
        log_path=path / "terminal.log",
        metrics_port=0,
        cwd=path,
        public_base_url="https://qa-bravo3.example",
        auth_mode="bearer",
        bearer_tokens="bravo3-isolated-token",
        mcp_auth_mode="",
        actions_auth_mode="",
        persistent_agents_enabled=True,
        fleet_instance_id="bravo3",
        fleet_id="",
        fleet_signing_private_key=base64.b64encode(bytes(range(32))).decode(),
        fleet_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        access_mesh_enabled=True,
        access_mesh_proof_key="qa-bravo3-fixture-not-production",
        access_mesh_peers_json="[]",
    )


def rpc(client, role, name, args, index):
    params = {
        "name": name,
        "arguments": args,
        "_meta": {"openai/subject": "bravo3-subject", "openai/session": f"qa-bravo3-{role}"},
    }
    resp = client.post(
        f"/terminal-mcp/{role}/v1/mcp/",
        headers={
            "authorization": "Bearer bravo3-isolated-token",
            "accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": params},
    )
    assert resp.status_code == 200, (role, name, resp.status_code, resp.text[:300])
    raw = resp.json()
    assert "result" in raw, raw
    result = raw["result"]
    assert not result["isError"], (role, name, result)
    payload = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == payload
    return (
        payload,
        len(resp.content),
        len(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()),
    )


def apply_future_catalogues(app):
    """Swap only discovery schemas/descriptions in an isolated process, no deploy."""
    for role in ROLES:
        server = getattr(app.state, f"{role}_mcp")
        intended = {item["name"]: item for item in CATALOGS[role]}
        for tool in server._tool_manager.list_tools():
            tool.parameters = intended[tool.name]["inputSchema"]
            tool.fn_metadata.output_schema = intended[tool.name]["outputSchema"]
            tool.description = intended[tool.name]["description"]
        actual = [
            t.model_dump(mode="json", by_alias=True, exclude_none=True)
            for t in asyncio.run(server.list_tools())
        ]
        assert actual == CATALOGS[role], role


def test_isolated_mcp_wire_20_invalid_and_many_valid(tmp_path):
    app = create_app(make_settings(tmp_path))
    apply_future_catalogues(app)
    error_rows = []
    with TestClient(app, base_url="https://qa-bravo3.example") as client:
        for i, (key, (valid, invalid)) in enumerate(CASES.items(), 1):
            role, name = key.split(".", 1)
            err, bytes_http, bytes_result = rpc(client, role, name, invalid, i * 2)
            assert err.get("ok") is False, (key, err)
            assert err["error"]["code"] == "input_validation_failed", (key, err)
            assert "inputSchema" not in json.dumps(err), (key, err)
            assert bytes_result < 3000, (key, bytes_result)
            error_rows.append(
                {
                    "tool": key,
                    "wire_bytes": bytes_http,
                    "result_bytes": bytes_result,
                    "structured_bytes": len(
                        json.dumps(
                            err, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ),
                    "error_bytes": len(
                        json.dumps(
                            err["error"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ),
                    "path": err["error"].get("path"),
                }
            )
            good, _, _ = rpc(client, role, name, valid, i * 2 + 1)
            assert good.get("error", {}).get("code") != "input_validation_failed", (key, good)
        # Stress bounded errors: long secret values, > 5 issue cap, nested path.
        leak = "QA_BRAVO3_DO_NOT_DISCLOSE_opaque_secret_" + "x" * 8192
        stress = [
            (
                "coordinator",
                "task_manage",
                {
                    "action": "create",
                    "namespace": "qa",
                    "isolation_hint": "isolated",
                    **{f"bad_{j}": leak for j in range(80)},
                },
            ),
            ("executor", "command_run", {"command": leak * 5}),
            ("executor", "message", {"action": "send", "message_hash": leak, "text": leak}),
            ("coordinator", "task_list", {"tags": [None] * 300}),
        ]
        for j, (role, name, args) in enumerate(stress, 200):
            obj, wire, size = rpc(client, role, name, args, j)
            assert obj["error"]["code"] == "input_validation_failed", (role, name, obj)
            assert leak not in json.dumps(obj)
            assert "inputSchema" not in json.dumps(obj)
            error_rows.append(
                {
                    "tool": f"STRESS:{role}.{name}",
                    "wire_bytes": wire,
                    "result_bytes": size,
                    "structured_bytes": len(
                        json.dumps(
                            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ),
                    "error_bytes": len(
                        json.dumps(
                            obj["error"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ),
                    "path": obj["error"].get("path"),
                }
            )
    # Keep historical QA wire evidence immutable; this invocation owns a temporary copy.
    dest = tmp_path / "qa-echo3-wire-metrics.json"
    dest.write_text(
        json.dumps({"qa_parent": SHA, "errors": error_rows}, indent=2) + "\n", encoding="utf-8"
    )
    assert len(error_rows) == 24
