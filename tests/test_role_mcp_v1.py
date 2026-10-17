import json
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.mcp.role_outputs import RoleHealthOutput, TaskClaimOutput
from terminal_mcp.mcp.roles import (
    _compact_health,
    build_role_mcp,
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


@pytest.mark.asyncio
async def test_current_task_claim_returns_the_compact_success_receipt(monkeypatch):
    from terminal_mcp.application.api import TerminalApplication

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
