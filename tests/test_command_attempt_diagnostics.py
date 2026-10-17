"""Isolated ASGI response loss, durable readback and independent attempts."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from terminal_mcp.attempt_diagnostics import install_role_attempt_diagnostics
from terminal_mcp.trace import TraceMiddleware, current_trace_id

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


def test_tool_attempt_event_survives_minimal_logging(tmp_path):
    from terminal_mcp.observability import EventLogger

    provider = SimpleNamespace(current=SimpleNamespace(detailed_logging=False))
    logger = EventLogger(tmp_path / "events.log", provider, metrics=None)
    logger.emit("mcp_tool_finished", role="executor")
    assert logger.queue.get_nowait().event["event"] == "mcp_tool_finished"
