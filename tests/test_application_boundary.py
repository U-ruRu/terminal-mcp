import ast
import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.application import ActorContext, TerminalApplication, get_application
from terminal_mcp.application.compatibility import ApplicationCompatibility
from terminal_mcp.application.requests import CmdRunRequest, ContextListRequest
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    current_admission_context,
    reset_admission_context,
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


def test_actor_is_immutable_and_copies_mutable_scopes():
    scopes = {"terminal:read"}
    value = ActorContext(scopes=scopes)
    scopes.add("terminal:execute")
    assert value.scopes == frozenset({"terminal:read"})
    with pytest.raises(FrozenInstanceError):
        value.principal_id = "other"
    with pytest.raises(ValueError, match="complete"):
        ActorContext(logical_agent_id="agent", work_session_id="session")
    with pytest.raises(ValueError, match="positive"):
        ActorContext(contract_version=0)


def test_anonymous_actor_does_not_inherit_ambient_privileges():
    token = bind_admission_context(actor("outer").admission())
    try:
        with ActorContext().bind():
            assert current_admission_context() is None
        assert current_admission_context().principal_id == "outer"
    finally:
        reset_admission_context(token)


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


@pytest.mark.asyncio
async def test_application_wrapper_is_reused_for_the_same_service():
    app = get_application(SimpleNamespace(persistent=None))
    assert get_application(app.service) is app
    assert get_application(app) is app


@pytest.mark.asyncio
async def test_session_start_without_backend_is_rejected():
    app = get_application(SimpleNamespace(persistent=None))
    assert (await app.session(ActorContext(), action="start", mode="legacy"))["code"] == (
        "policy_incompatible"
    )


@pytest.mark.asyncio
async def test_context_list_projects_the_current_backend_result():
    service = Service()
    result = await get_application(service).context(
        ActorContext(), ContextListRequest(action="list")
    )
    assert result == {"ok": True, "primary": [], "additional": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_active_facade_rejects_an_unknown_operation():
    facade = ApplicationCompatibility(object())
    with pytest.raises(ValueError):
        await facade.call(actor(), "__getattribute__", "secret")


@pytest.mark.asyncio
async def test_active_facade_restores_admission_context_after_cancellation():
    class CancelledService:
        async def health(self):
            assert current_admission_context().principal_id == "inside"
            raise asyncio.CancelledError

    before = current_admission_context()
    facade = ApplicationCompatibility(CancelledService())
    with pytest.raises(asyncio.CancelledError):
        await facade.call(actor("inside"), "health")
    assert current_admission_context() is before


def test_application_modules_never_import_inbound_transports_or_sql_drivers():
    directory = Path(__file__).parents[1] / "src/terminal_mcp/application"
    prohibited = (
        "mcp",
        "fastapi",
        "starlette",
        "aiosqlite",
        "sqlite3",
        "terminal_mcp.mcp",
        "terminal_mcp.http",
        "terminal_mcp.adapters",
    )
    violations = []
    for path in directory.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if any(name == prefix or name.startswith(prefix + ".") for prefix in prohibited):
                    violations.append((path.name, node.lineno, name))
    assert not violations
