import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import CapabilityPolicy
from terminal_mcp.mcp.role_contracts import ROLE_TOOL_MODELS, planning_schema
from terminal_mcp.mcp.role_outputs import RoleHealthOutput, TaskClaimOutput
from terminal_mcp.mcp.roles import (
    COORDINATOR_TOOLS,
    EXECUTOR_TOOLS,
    _compact_health,
    build_role_mcp,
    role_schema_contract,
)


def _service():
    return SimpleNamespace(persistent=None)


def _tool_map(role):
    return {item.name: item for item in build_role_mcp(_service(), role)._tool_manager.list_tools()}


def test_role_catalogs_are_exact_and_identity_free():
    for role, expected in (("executor", EXECUTOR_TOOLS), ("coordinator", COORDINATOR_TOOLS)):
        tools = _tool_map(role)
        assert list(tools) == list(expected)
        assert len(tools) == len(expected)
        for _name, tool in tools.items():
            schema = tool.parameters
            assert schema["type"] == "object"
            assert "properties" in schema
            if role == "executor" and _name == "task_state":
                assert set(schema.get("required", [])) == {"namespace", "task_id", "state"}
            else:
                assert "required" not in schema
            forbidden = {
                "code",
                "sender",
                "logical_agent_id",
                "work_session_id",
                "session_epoch",
                "endpoint_role",
                "contract_version",
            }
            assert forbidden.isdisjoint(schema["properties"])
            assert "required" not in (tool.description or "").lower()


def test_task_claim_output_accepts_session_lifecycle_and_nullable_checkpoint():
    lifecycle = {
        "state": "active",
        "remaining_seconds": 1200,
        "hard_expires_at": "2026-10-07T20:30:00Z",
        "return_to_chat": False,
    }
    claim_task = {
        "namespace": "live",
        "task_id": "T-1",
        "title": "Probe",
        "lane": "general",
        "priority": "P3",
        "state": "ready",
        "operational_status": "ready",
        "revision": 1,
        "claim": None,
        "next_action": "",
        "description_preview": "",
        "description_truncated": False,
        "blocking_dependencies": [],
        "recent_comments": [],
        "recent_checkpoints": [],
    }
    claimed = TaskClaimOutput.model_validate(
        {
            "ok": True,
            "action": "claim",
            "task": claim_task,
            "warnings": [],
            "session_lifecycle": lifecycle,
        }
    )
    assert claimed.root.task.latest_checkpoint is None
    released = TaskClaimOutput.model_validate(
        {
            "ok": True,
            "action": "release",
            "task": {
                "namespace": "live",
                "task_id": "T-1",
                "revision": 1,
                "state": "ready",
                "operational_status": "ready",
            },
            "warnings": [],
            "session_lifecycle": lifecycle,
        }
    )
    assert released.root.session_lifecycle.remaining_seconds == 1200


def test_role_tools_publish_output_schemas():
    for role in ("executor", "coordinator"):
        for name, tool in _tool_map(role).items():
            assert isinstance(tool.output_schema, dict), (role, name)
            assert tool.output_schema


def test_planning_schema_is_typed_relaxed_runtime_superset():
    for _key, model in ROLE_TOOL_MODELS.items():
        schema = planning_schema(model)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        runtime = model.model_json_schema()
        if model.__name__ in {"TaskStateInput", "AttachInput"}:
            assert schema["required"]
        else:
            assert "required" not in schema
        assert set(runtime.get("properties", ())) == set(schema.get("properties", ()))


@pytest.mark.asyncio
async def test_structural_errors_reach_authoritative_validator():
    tools = _tool_map("executor")
    cases = (
        ("command_run", {}, "missing_required", "command"),
        ("command_cancel", {"cmd_hash": "x", "bogus": 1}, "unexpected_field", "*"),
        ("message", {"action": "reply", "text": "hello"}, "constraint_violation", "message_hash"),
    )
    for name, args, reason, path in cases:
        result = await tools[name].run(args, convert_result=True)
        payload = result.structuredContent
        assert result.isError is False
        assert payload["ok"] is False
        error = payload["error"]
        assert error["code"] == "input_validation_failed"
        assert error["outcome"] == "not_committed"
        assert error["retry"] == "repair"
        assert error["reason"] == reason
        assert error["path"] == path
        assert error["details"]["validation_errors"]
        assert json.loads(result.content[0].text) == payload
        assert len(json.dumps(payload, separators=(",", ":")).encode()) < 1024


def test_compact_health_matches_closed_role_output_contract():
    raw = {
        "ok": True,
        "application": "terminal-mcp",
        "version": "0.13.1",
        "storage": "ok",
        "status": "healthy",
        "components": [{"id": "storage", "status": "healthy", "reason": None}],
        "terminal": {
            "ok": True,
            "scheduler": "numbered-fifo",
            "parallelism": 4,
            "queue_size": 0,
            "degraded": False,
        },
        "workflow": {
            "ok": True,
            "by_state": {"ready": 10},
            "active_claims": 2,
            "unreleased_claims": 1,
            "live_claims": 2,
            "stale_claims": 0,
        },
    }

    compact = _compact_health(raw)

    assert compact["workflow"] == {
        "ok": True,
        "active_claims": 2,
        "unreleased_claims": 1,
        "stale_claims": 0,
    }
    RoleHealthOutput.model_validate(compact)


def test_application_role_authorization_sets():
    policy = CapabilityPolicy()
    executor = ActorContext(endpoint_role="executor", contract_version=1)
    coordinator = ActorContext(endpoint_role="coordinator", contract_version=1)

    assert policy.allows(executor, "commands")
    assert not policy.allows(executor, "health")
    assert policy.allows_task_action(executor, "claim")
    assert not policy.allows_task_action(executor, "create")

    assert not policy.allows(coordinator, "commands")
    assert policy.allows(coordinator, "health")
    assert policy.allows_task_action(coordinator, "create")
    assert not policy.allows_task_action(coordinator, "claim")


def test_schema_release_baseline_is_exact():
    path = Path("src/terminal_mcp/mcp/role_schema_baselines_v1.json")
    baseline = json.loads(path.read_text())
    actual = {
        "contract_version": 1,
        "roles": {
            role: role_schema_contract(role)["tools"] for role in ("executor", "coordinator")
        },
    }
    assert actual == baseline


def test_planning_schema_accepts_invalid_values_for_runtime_validation():
    from jsonschema import Draft202012Validator

    for role in ("executor", "coordinator"):
        for name, tool in _tool_map(role).items():
            validator = Draft202012Validator(tool.parameters)
            if role == "executor" and name == "task_state":
                assert list(validator.iter_errors({}))
                validator.validate({"namespace": "x", "task_id": "y", "state": "qa"})
            else:
                validator.validate({})
                validator.validate(
                    {key: {"malformed": [None, 12]}
                     for key in tool.parameters["properties"]}
                )
                validator.validate({"unexpected_field": "runtime owns validation"})
    assert "isolation_hint" in _tool_map("coordinator")["task_manage"].description
    assert "claim_intent" in _tool_map("executor")["task_claim"].description


def test_session_bound_tool_descriptions_name_session_start_prerequisite():
    for role, name in (
        ("executor", "task_claim"),
        ("executor", "command_recovery"),
        ("executor", "message"),
        ("coordinator", "task_manage"),
        ("coordinator", "agent_observe"),
        ("coordinator", "message"),
    ):
        assert "session(action='start')" in _tool_map(role)[name].description


@pytest.mark.asyncio
async def test_role_failure_preserves_all_validation_issues_and_matches_wire_schema():
    from jsonschema import Draft202012Validator

    tool = _tool_map("executor")["task_claim"]
    result = await tool.run({"action": "claim", "namespace": 12}, convert_result=True)
    assert result.isError is False
    error = result.structuredContent["error"]
    paths = {item["path"] for item in error["details"]["validation_errors"]}
    assert paths == {"namespace", "task_id"}
    Draft202012Validator(tool.output_schema).validate(result.structuredContent)


@pytest.mark.asyncio
async def test_role_runtime_exception_is_structured_and_private(monkeypatch):
    from jsonschema import Draft202012Validator

    import terminal_mcp.mcp.roles as roles

    async def failing(*args, **kwargs):
        raise RuntimeError("PRIVATE-TOKEN-must-stay-in-logs")

    monkeypatch.setattr(roles, "_task_list", failing)
    tool = _tool_map("executor")["task_list"]
    result = await tool.run({}, convert_result=True)
    assert result.isError is False
    assert result.structuredContent["error"]["code"] == "internal_error"
    assert result.structuredContent["error"]["outcome"] == "unknown"
    assert "PRIVATE-TOKEN" not in result.model_dump_json()
    Draft202012Validator(tool.output_schema).validate(result.structuredContent)


@pytest.mark.asyncio
async def test_activity_touch_failure_preserves_success(monkeypatch):
    import terminal_mcp.mcp.roles as roles
    from terminal_mcp.application.session_gate import SessionGate

    async def succeed(*args, **kwargs):
        return {"ok": True, "tasks": [], "next_cursor": None}

    async def fail_touch(*args, **kwargs):
        raise ConnectionError("activity telemetry unavailable")

    monkeypatch.setattr(roles, "_task_list", succeed)
    monkeypatch.setattr(SessionGate, "touch_provider", fail_touch)
    result = await _tool_map("executor")["task_list"].run({}, convert_result=True)
    assert result.isError is False
    assert result.structuredContent["ok"] is True


@pytest.mark.asyncio
async def test_health_identity_diagnostic_uses_metadata_without_a_session():
    from terminal_mcp.mcp.roles import _health_identity

    class Gate:
        async def provider_identity(self, actor, operation):
            return SimpleNamespace(
                failure=None,
                identity={
                    "logical_agent_id": "la_test",
                    "public_name": "Test-1",
                    "authority_node_id": "firstbyte",
                },
            )

    actor = ActorContext(
        provider="openai",
        provider_metadata={
            "openai/subject": "private-subject",
            "openai/session": "private-conversation",
        },
    )
    result = await _health_identity(SimpleNamespace(session_gate=Gate()), actor)
    assert result["status"] == "resolved"
    assert result["metadata_fields"] == ["openai/session", "openai/subject"]
    assert result["public_name"] == "Test-1"
    assert len(result["binding_key"]) == 64
    assert "private-" not in json.dumps(result)


@pytest.mark.asyncio
async def test_health_identity_failure_preserves_diagnostic_access():
    from terminal_mcp.mcp.roles import _health_identity

    class Gate:
        async def provider_identity(self, actor, operation):
            raise ConnectionError("private-host")

    actor = ActorContext(
        provider="openai",
        provider_metadata={"openai/subject": "subject", "openai/session": "conversation"},
    )
    result = await _health_identity(SimpleNamespace(session_gate=Gate()), actor)
    assert result["status"] == "unavailable"
    assert "private-host" not in json.dumps(result)


def test_extended_role_health_omits_host_privilege_details():
    from terminal_mcp.mcp.roles import _compact_health

    raw = {
        "ok": True,
        "terminal": {"ok": True, "uid": 0, "cwd": "/private", "shell": "/bin/bash"},
        "custom_command": {"command": "private command"},
    }
    public = _compact_health(raw)
    assert public["terminal"] == {"ok": True}
    assert "private" not in json.dumps(public)


@pytest.mark.asyncio
async def test_stalled_activity_touch_is_bounded_after_success(monkeypatch):
    import asyncio

    import terminal_mcp.mcp.roles as roles
    from terminal_mcp.application.session_gate import SessionGate

    async def succeed(*args, **kwargs):
        return {"ok": True, "tasks": [], "next_cursor": None}

    async def stalled(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(roles, "_task_list", succeed)
    monkeypatch.setattr(SessionGate, "touch_provider", stalled)
    async with asyncio.timeout(2):
        result = await _tool_map("executor")["task_list"].run({}, convert_result=True)
    assert result.structuredContent["ok"] is True


def test_graph_output_keeps_nullable_state_for_every_node():
    from terminal_mcp.mcp.output_contracts import projected_result
    from terminal_mcp.mcp.role_outputs import TaskGraphOutput

    raw = {
        "ok": True,
        "nodes": [
            {"namespace": "test", "task_id": "root", "state": "ready"},
            {"namespace": "test", "task_id": "related", "state": None},
        ],
        "edges": [],
    }
    result = projected_result(raw, TaskGraphOutput, tool="task_graph", variant="coordinator")
    assert all(
        set(node) == {"namespace", "task_id", "state"} for node in result.structuredContent["nodes"]
    )
    assert result.structuredContent["nodes"][1]["state"] is None


def test_unhealthy_components_are_a_successful_diagnostic_result():
    from terminal_mcp.mcp.output_contracts import projected_result
    from terminal_mcp.mcp.role_outputs import RoleHealthOutput
    from terminal_mcp.mcp.roles import _compact_health

    raw = {
        "ok": False,
        "status": "failed",
        "terminal": {"ok": False},
        "components": [{"id": "terminal", "status": "failed"}],
    }
    result = projected_result(
        _compact_health(raw), RoleHealthOutput, tool="health", variant="coordinator"
    )
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["healthy"] is False
    assert result.structuredContent["status"] == "failed"


@pytest.mark.asyncio
async def test_session_start_exposes_only_hashed_provider_evidence(monkeypatch):
    import terminal_mcp.mcp.role_outputs as output
    from terminal_mcp.adapters.mcp_identity import ProviderRequestEvidence
    from terminal_mcp.application.api import TerminalApplication
    from terminal_mcp.core.provider_identity import ProviderIdentityRegistry

    metadata = {"openai/subject": "private-user", "openai/session": "private-chat"}

    async def start(*args, **kwargs):
        return {
            "ok": True,
            "action": "start",
            "public_name": "Test-1",
            "mode": "persistent",
            "session_state": "active",
            "session_ref": "ws_test",
        }

    monkeypatch.setattr(TerminalApplication, "session", start)
    monkeypatch.setattr(
        output, "current_provider_evidence", lambda: ProviderRequestEvidence("openai", metadata)
    )
    tool = _tool_map("executor")["session"]
    result = await tool.run({"action": "start"}, convert_result=True)
    assert result.isError is False
    assert result.structuredContent["ok"] is True
    diagnostic = result.structuredContent["provider_identity"]
    assert (
        diagnostic["binding_key"]
        == ProviderIdentityRegistry().resolve("openai", metadata).binding_key
    )
    assert len(diagnostic["subject_fingerprint"]) == 64
    assert len(diagnostic["session_fingerprint"]) == 64
    assert "private-" not in result.model_dump_json()
    from jsonschema import Draft202012Validator

    Draft202012Validator(tool.output_schema).validate(result.structuredContent)


@pytest.mark.asyncio
async def test_role_contract_rejection_is_structured_and_preserves_identity_fingerprint(
    monkeypatch,
):
    import terminal_mcp.mcp.role_outputs as outputs
    import terminal_mcp.mcp.roles as roles
    from terminal_mcp.adapters.mcp_identity import ProviderRequestEvidence
    from terminal_mcp.core.work_windows import WorkWindowError

    async def reject(*args, **kwargs):
        raise WorkWindowError("session_contract_conflict")

    monkeypatch.setattr(roles, "_task_list", reject)
    monkeypatch.setattr(
        outputs,
        "current_provider_evidence",
        lambda: ProviderRequestEvidence(
            "openai", {"openai/subject": "private-subject", "openai/session": "private-session"}
        ),
    )
    tool = _tool_map("executor")["task_list"]
    result = await tool.run({}, convert_result=True)
    assert result.isError is False
    payload = result.structuredContent
    assert payload["error"]["code"] == "session_contract_conflict"
    assert payload["error"]["outcome"] == "not_committed"
    assert len(payload["provider_identity"]["binding_key"]) == 64
    assert "private-" not in result.model_dump_json()
