"""Isolated ASGI response loss, durable readback and independent attempts."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import terminal_mcp.core.persistent_backend as backend_module
from terminal_mcp.attempt_diagnostics import install_role_attempt_diagnostics
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.mcp.output_contracts import cmd_result
from terminal_mcp.mcp.roles import build_role_mcp
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.trace import TraceMiddleware, current_trace_id, current_upstream_request_id

SECRET = "do-not-log-body-headers-output"
IDENTITY = dict(logical_agent_id="safe-agent", work_session_id="safe-session", session_epoch=1)


class Events:
    def __init__(self, broken=False):
        self.rows = []
        self.broken = broken

    def emit(self, event, **fields):
        if self.broken:
            raise RuntimeError(SECRET)
        self.rows.append(dict(event=event, trace_id=current_trace_id.get(), **fields))


class Lifecycle:
    @asynccontextmanager
    async def operation_guard(self, *args):
        yield


def instrument(fn, events, name="command_run", role="executor"):
    tool = SimpleNamespace(name=name, fn=fn)
    mcp = SimpleNamespace(_tool_manager=SimpleNamespace(list_tools=lambda: [tool]))
    install_role_attempt_diagnostics(mcp, SimpleNamespace(events=events), role)
    return tool


async def request(app, *, fail_body=False, role="executor"):
    scope = {
        "type": "http",
        "path": f"/terminal-mcp/{role}/v1/mcp/",
        "headers": [(b"x-request-id", SECRET.encode()), (b"authorization", SECRET.encode())],
    }
    # Reusing a JSON-RPC ID does not identify a logical operation.
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "params": {"command": SECRET}}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if fail_body and message["type"] == "http.response.body":
            raise OSError(SECRET)

    return await app(scope, receive, send)


def adapter(tool):
    async def app(scope, receive, send):
        await receive()
        result = await tool.fn()
        value = result if isinstance(result, dict) else result.structuredContent
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": json.dumps(value).encode()})

    return app


@pytest.mark.asyncio
async def test_lost_response_keeps_command_and_same_rpc_id_runs_independently(
    tmp_path, monkeypatch
):
    repo = SqliteRepository(tmp_path / "commands.db", tmp_path / "output.db")
    await repo.initialize()
    terminal = SimpleNamespace(
        queue_workers=1,
        least_loaded_queue=AsyncMock(return_value=1),
        submit=AsyncMock(),
        enqueue_cancel=Mock(),
    )
    service = SimpleNamespace(
        repo=repo,
        terminal=terminal,
        task_coordinator=None,
        task_store=None,
        agent_policy=SimpleNamespace(command_preview_chars=160),
    )
    backend = PersistentBackend(service, Lifecycle())
    backend._execution_authority = AsyncMock(
        return_value=(
            SimpleNamespace(hard_expires_at="2099-01-01T00:00:00Z", authority_node_id="local"),
            None,
        )
    )
    backend._task_refs = AsyncMock(return_value=[])
    monkeypatch.setattr(backend_module, "COMMAND_RUN_INLINE_BUDGET_SECONDS", 0)

    async def run():
        return cmd_result(
            await backend.run("printf safe", **IDENTITY, queue_id=1, task_scope="none"), "run"
        )

    events = Events()
    app = TraceMiddleware(adapter(instrument(run, events)), events=events)
    before = (current_trace_id.get(), current_upstream_request_id.get())
    with pytest.raises(OSError):
        await request(app, fail_body=True)
    first = [r for r in events.rows if r["event"] == "mcp_tool_finished"][-1]
    assert first["result_ok"] is True
    assert (await repo.get(first["cmd_hash"])) is not None
    assert events.rows[-1]["response_started"] is True
    assert events.rows[-1]["response_complete"] is False
    assert events.rows[-1]["exception_class"] == "OSError"
    # The first durable command remains recoverable; a separate invocation is not merged.
    await request(app)
    finishes = [r for r in events.rows if r["event"] == "mcp_tool_finished"]
    assert len(finishes) == 2 and len({r["cmd_hash"] for r in finishes}) == 2
    assert len({r["tool_attempt_id"] for r in finishes}) == 2
    assert len({r["trace_id"] for r in finishes}) == 2
    assert terminal.submit.await_count == 2
    assert events.rows[-1]["response_complete"] is True
    assert (current_trace_id.get(), current_upstream_request_id.get()) == before
    assert SECRET not in json.dumps(events.rows)
    assert all(r.get("upstream_request_id") is None for r in events.rows)


@pytest.mark.asyncio
async def test_task_commit_is_readable_after_lost_asgi_body(tmp_path):
    repo = SqliteRepository(tmp_path / "tasks.db", tmp_path / "output.db")
    await repo.initialize()
    store = TaskStore(repo.path)
    tasks = TaskCoordinator(store)
    key = dict(namespace="live021", task_id="safe-task")
    await tasks.mutate("author", action="create", title="Safe", isolation_hint="none", **key)

    async def update():
        return await tasks.mutate(
            "author", action="update", **key, expected_revision=1, description="CONTENT-A-safe"
        )

    events = Events()
    app = TraceMiddleware(
        adapter(instrument(update, events, "task_manage", "coordinator")), events=events
    )
    with pytest.raises(OSError):
        await request(app, fail_body=True, role="coordinator")
    reopened = TaskStore(repo.path)
    saved = await reopened.get_task(**key)
    assert saved["revision"] == 2 and saved["description"] == "CONTENT-A-safe"
    result = [r for r in events.rows if r["event"] == "mcp_tool_finished"][0]
    assert result["result_ok"] is True and result["task_revision"] == 2
    assert result["task_id"] == "safe-task"
    assert len({r["trace_id"] for r in events.rows}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_broken_diagnostics_preserve_result_or_cancellation(cancel):
    async def run():
        if cancel:
            raise asyncio.CancelledError
        return {"ok": True, "command": {"cmd_hash": "1234abcd", "stdout": SECRET}}

    events = Events(broken=True)
    app = TraceMiddleware(adapter(instrument(run, events)), events=events)
    before = (current_trace_id.get(), current_upstream_request_id.get())
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await request(app)
    else:
        await request(app)
    assert (current_trace_id.get(), current_upstream_request_id.get()) == before


@pytest.mark.asyncio
async def test_timeout_is_reported_and_context_resets():
    async def app(scope, receive, send):
        await asyncio.sleep(1)

    events = Events()
    before = current_trace_id.get()
    with pytest.raises(TimeoutError):
        await request(TraceMiddleware(app, timeout_seconds=0.01, events=events))
    assert events.rows[-1]["outcome"] == "timeout"
    assert events.rows[-1]["response_complete"] is False
    assert current_trace_id.get() == before


@pytest.mark.asyncio
async def test_real_role_catalog_installs_diagnostics_without_schema_change():
    events = Events()
    mcp = build_role_mcp(SimpleNamespace(persistent=None, events=events), "executor")
    plain = build_role_mcp(SimpleNamespace(persistent=None), "executor")
    assert [(t.name, t.parameters, t.output_schema) for t in mcp._tool_manager.list_tools()] == [
        (t.name, t.parameters, t.output_schema) for t in plain._tool_manager.list_tools()
    ]
    result = await mcp.call_tool("command_run", {})
    assert result.structuredContent["ok"] is False
    assert [r["event"] for r in events.rows] == ["mcp_tool_started", "mcp_tool_finished"]


@pytest.mark.asyncio
async def test_application_timeout_is_not_misattributed_to_http_deadline():
    async def app(scope, receive, send):
        raise TimeoutError("application-specific timeout")

    events = Events()
    with pytest.raises(TimeoutError, match="application-specific"):
        await request(TraceMiddleware(app, events=events))
    assert events.rows[-1]["outcome"] == "exception"
    assert events.rows[-1]["exception_class"] == "TimeoutError"


@pytest.mark.asyncio
async def test_concurrent_requests_have_disjoint_trace_and_tool_attempts():
    async def run():
        await asyncio.sleep(0)
        return {"ok": True}

    events = Events()
    app = TraceMiddleware(adapter(instrument(run, events)), events=events)
    await asyncio.gather(request(app), request(app))
    traces = {r["trace_id"] for r in events.rows}
    assert len(traces) == 2
    for trace in traces:
        rows = [r for r in events.rows if r["trace_id"] == trace]
        assert [r["event"] for r in rows] == [
            "mcp_http_started",
            "mcp_tool_started",
            "mcp_tool_finished",
            "mcp_http_finished",
        ]
        assert rows[1]["tool_attempt_id"] == rows[2]["tool_attempt_id"]


@pytest.mark.asyncio
async def test_broken_sink_preserves_original_application_exception():
    async def run():
        raise ValueError("original exception")

    events = Events(broken=True)
    app = TraceMiddleware(adapter(instrument(run, events)), events=events)
    with pytest.raises(ValueError, match="original exception"):
        await request(app)


@pytest.mark.parametrize(
    "event", ["mcp_http_started", "mcp_http_finished", "mcp_tool_started", "mcp_tool_finished"]
)
def test_attempt_events_survive_minimal_logging(tmp_path, event):
    from terminal_mcp.observability import EventLogger

    provider = SimpleNamespace(current=SimpleNamespace(detailed_logging=False))
    logger = EventLogger(tmp_path / "events.log", provider, metrics=None)
    logger.emit(event, role="executor")
    assert logger.queue.get_nowait().event["event"] == event
