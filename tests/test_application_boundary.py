import asyncio

import pytest

from terminal_mcp.application import ActorContext, TerminalApplication
from terminal_mcp.application.requests import CmdRunRequest
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    current_admission_context,
)


def actor(name="owner", **metadata):
    return ActorContext.from_admission(
        VerifiedAdmissionContext(
            name, f"credential:{name}", frozenset({"terminal:execute"}), 1, "internal", "bearer"
        ),
        node_id="node-a",
        **metadata,
    )


class Backend:
    def __init__(self):
        self.calls = []
        self.state = {"alert_pending": False, "ack_required_pending": False}

    async def access_identity(self, code):
        await asyncio.sleep(0)
        self.calls.append(("identity", current_admission_context().principal_id))
        return {
            "ok": True,
            "logical_agent_id": "agent",
            "work_session_id": "session",
            "session_epoch": 2,
            "authority_node_id": "node-authority",
        }

    async def message_state(self, **kwargs):
        await asyncio.sleep(0)
        return dict(self.state)

    async def run(self, command, **kwargs):
        await asyncio.sleep(0)
        self.calls.append(("run", current_admission_context().principal_id))
        return {"ok": True, "cmd_hash": "hash", "command": command}


class Service:
    def __init__(self):
        self.persistent = Backend()

    async def context(self, action, **kwargs):
        return {"ok": True, "primary": [], "additional": []}

    async def health(self, auth_mode="none", **kwargs):
        return {"ok": True, "auth_mode": auth_mode}


@pytest.mark.asyncio
async def test_concurrent_application_calls_keep_separate_actor_contexts():
    service = Service()
    app = TerminalApplication(service)
    before = current_admission_context()
    request = CmdRunRequest(action="run", code="0042", command="printf safe")
    values = await asyncio.gather(app.cmd(actor("alice"), request), app.cmd(actor("bob"), request))
    assert all(value["ok"] for value in values)
    assert sorted(service.persistent.calls) == [
        ("identity", "alice"),
        ("identity", "bob"),
        ("run", "alice"),
        ("run", "bob"),
    ]
    assert current_admission_context() is before


@pytest.mark.asyncio
async def test_session_gate_enriches_from_authority_without_mutating_input_actor():
    original = actor()
    result = await TerminalApplication(Service()).session_gate.resolve(original, "0042")
    assert result.failure is None
    assert result.actor.authority_node_id == "node-authority"
    assert result.actor.logical_agent_id == "agent"
    assert result.actor.session_epoch == 2
    assert original.logical_agent_id is None


@pytest.mark.asyncio
async def test_capability_policy_denies_coordinator_execution_before_side_effects():
    service = Service()
    app = TerminalApplication(service)
    result = await app.cmd(
        actor(endpoint_role="coordinator"),
        CmdRunRequest(action="run", code="0042", command="printf blocked"),
    )
    assert result["code"] == "capability_not_allowed"
    assert service.persistent.calls == []
