import ast
import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.application import ActorContext, TerminalApplication, get_application
from terminal_mcp.application.compatibility import ApplicationCompatibility
from terminal_mcp.application.requests import CmdRunRequest, ContextListRequest
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    current_admission_context,
    reset_admission_context,
)
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.trace import current_trace_id


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
        ActorContext(logical_agent_id="agent")
    with pytest.raises(ValueError, match="positive"):
        ActorContext(contract_version=0)


def test_actor_factory_uses_verified_identity_and_server_trace():
    token = bind_admission_context(actor().admission())
    trace_token = current_trace_id.set("server-trace")
    try:
        service = Service()
        service.persistent.lifecycle = SimpleNamespace(authority_node_id="node-a")
        value = actor_for(service, transport="mcp", endpoint_role="executor", contract_version=1)
        assert value.principal_id == "owner"
        assert value.node_id == "node-a"
        assert value.transport == "mcp"
        assert value.request_id == "server-trace"
        assert value.endpoint_role == "executor"
        assert value.logical_agent_id is None
    finally:
        current_trace_id.reset(trace_token)
        reset_admission_context(token)


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
async def test_application_reads_and_missing_backend_preserve_compatibility():
    app = get_application(SimpleNamespace(persistent=None))
    assert get_application(app.service) is app
    assert (await app.session(ActorContext(), action="start", mode="legacy"))["code"] == (
        "policy_incompatible"
    )
    service = Service()
    result = await get_application(service).context(
        ActorContext(), ContextListRequest(action="list")
    )
    assert result == {"ok": True, "primary": [], "additional": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_legacy_facade_is_allowlisted_and_restores_actor_on_cancellation():
    class CancelledService:
        async def health(self):
            assert current_admission_context().principal_id == "inside"
            raise asyncio.CancelledError

    before = current_admission_context()
    facade = ApplicationCompatibility(CancelledService())
    with pytest.raises(ValueError):
        await facade.call(actor(), "__getattribute__", "secret")
    with pytest.raises(asyncio.CancelledError):
        await facade.call(actor("inside"), "health")
    assert current_admission_context() is before


@pytest.mark.asyncio
async def test_architecture_a_preserves_exact_published_mcp_contracts():
    expected = json.loads(
        (Path(__file__).parent / "fixtures/architecture_a_mcp_contract.json").read_text()
    )
    actual = {}
    for tool in await build_mcp(SimpleNamespace(persistent=None)).list_tools():
        raw = tool.model_dump(mode="json", exclude_none=True)
        actual[tool.name] = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for forbidden in ("principal_id", "credential_id", "endpoint_role", "provider", "node_id"):
            assert forbidden not in tool.inputSchema.get("properties", {})
    assert actual == expected


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


def test_mcp_handlers_do_not_resolve_identity_or_access_storage():
    path = Path(__file__).parents[1] / "src/terminal_mcp/mcp/server.py"
    prohibited = {
        "access_identity",
        "access_message",
        "access_session_start",
        "task_store",
        "context_store",
        "persistent",
        "message_state",
        "authorize_session",
    }
    assert not [
        node.attr
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr in prohibited
    ]


@pytest.mark.asyncio
async def test_mcp_adapter_accepts_canonical_application_directly():
    service = Service()
    service.persistent.lifecycle = SimpleNamespace(authority_node_id="node-direct")
    application = TerminalApplication(service)
    assert get_application(application) is application
    assert actor_for(application, transport="mcp").node_id == "node-direct"
    expected = await build_mcp(service).list_tools()
    actual = await build_mcp(application).list_tools()
    assert [item.model_dump(mode="json") for item in actual] == [
        item.model_dump(mode="json") for item in expected
    ]


def test_previous_projection_imports_remain_compatibility_aliases():
    from terminal_mcp.application.projections import _task_summary as canonical
    from terminal_mcp.mcp.server import _task_summary as compatibility

    assert compatibility is canonical
