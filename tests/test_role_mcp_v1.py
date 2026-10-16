import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import CapabilityPolicy
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
    return {
        item.name: item
        for item in build_role_mcp(
            _service(), role, publish_agent_contract=False
        )._tool_manager.list_tools()
    }


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


@pytest.mark.asyncio
async def test_task_claim_output_accepts_session_lifecycle_and_nullable_checkpoint(monkeypatch):
    from terminal_mcp.application.api import TerminalApplication
    from terminal_mcp.core.task_projections import TaskSnapshot
    from terminal_mcp.mcp.output_contracts import SessionLifecycleInfo

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
    # The internal nullable snapshot and lifecycle remain valid. They no longer
    # belong to a successful public claim/release receipt.
    snapshot = TaskSnapshot.model_validate(
        {key: value for key, value in claim_task.items() if key in TaskSnapshot.model_fields}
    )
    assert snapshot.latest_checkpoint is None
    assert SessionLifecycleInfo.model_validate(lifecycle).remaining_seconds == 1200

    async def succeed(*args, **kwargs):
        return {
            "ok": True,
            "action": "claim",
            "task": claim_task,
            "warnings": [],
            "session_lifecycle": lifecycle,
        }

    monkeypatch.setattr(TerminalApplication, "task", succeed)
    result = await _tool_map("executor")["task_claim"].run(
        {
            "action": "claim",
            "namespace": "live",
            "task_id": "T-1",
            "claim_intent": "review",
        },
        convert_result=True,
    )
    assert result.isError is False
    assert result.structuredContent == {"ok": True}
    claimed = TaskClaimOutput.model_validate(result.structuredContent)
    assert claimed.root.ok is True








@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", ["invalid", 42])
async def test_task_state_invalid_input_has_structured_repair_and_no_domain_call(
    monkeypatch, bad_state
):
    from terminal_mcp.application.api import TerminalApplication

    async def forbidden_task(*args, **kwargs):
        raise AssertionError("Malformed state reached the domain mutation")

    monkeypatch.setattr(TerminalApplication, "task", forbidden_task)
    result = await _tool_map("executor")["task_state"].run(
        {"namespace": "smoke", "task_id": "T-1", "state": bad_state},
        convert_result=True,
    )
    assert result.isError is False
    data = result.structuredContent
    assert data["ok"] is False
    assert data["outcome"] == "not_committed"
    error = data["error"]
    assert error["code"] == "input_validation_failed"
    assert error["reason"] == "invalid_value"
    assert error["path"] == "state"
    assert error["expected"] == "one of: ready|in_progress|qa|blocked|deferred|done"
    assert error["details"]["validation_errors"][0]["expected"] == error["expected"]
    assert json.loads(result.content[0].text) == data






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


def test_short_health_aggregates_diagnostics_hidden_from_the_public_body():
    raw = {
        "ok": True,
        "components": [{"id": "mandatory-worker", "status": "failed"}],
        "terminal": {"ok": False},
        "workflow": {"ok": False},
    }
    public = _compact_health(raw)
    assert public["ok"] is True
    assert public["healthy"] is False
    assert {"components", "terminal", "workflow", "status"}.isdisjoint(public)
    RoleHealthOutput.model_validate(public)
