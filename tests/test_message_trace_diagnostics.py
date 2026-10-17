"""LIVE022: local routing, acknowledgment semantics and bounded receipt observability."""

from types import SimpleNamespace

import pytest
from test_access_mesh_messages_v2 import actor as actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair_fixture

from terminal_mcp.attempt_diagnostics import (
    install_role_attempt_diagnostics,
    traced_task_mutation,
    traced_task_response,
)
from terminal_mcp.trace import current_trace_id

mesh_pair = mesh_pair_fixture


def _row(local, msg):
    with local.store.connect() as db:
        return dict(
            db.execute(
                "SELECT first_seen_at, read_at, replied_at FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id='carol'",
                (msg,),
            ).fetchone()
        )


def _count(local):
    with local.store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM access_mesh_messages").fetchone()[0]


@pytest.mark.asyncio
async def test_task_mutation_trace_links_existing_tool_attempt_and_response_without_payload():
    class Events:
        def __init__(self):
            self.rows = []

        def emit(self, event, **fields):
            self.rows.append({"event": event, "trace_id": current_trace_id.get(), **fields})

    events = Events()
    canonical = SimpleNamespace(action="state", namespace="safe-namespace", task_id="safe-task")
    secret = "do-not-log-auth-or-task-body"

    class Application:
        service = SimpleNamespace(events=events)

        async def task(self, actor, request):
            assert actor == "agent" and request is canonical
            return {"ok": True, "task": {"task_id": "safe-task", "revision": 12}, "secret": secret}

    app = Application()

    async def tool():
        raw, started = await traced_task_mutation(
            app, "agent", canonical, role="executor", tool="task_state"
        )
        return traced_task_response(
            app, canonical, raw, {"ok": True}, started, role="executor", tool="task_state"
        )

    wrapped = SimpleNamespace(name="task_state", fn=tool)
    mock_mcp = SimpleNamespace(_tool_manager=SimpleNamespace(list_tools=lambda: [wrapped]))
    install_role_attempt_diagnostics(mock_mcp, app.service, "executor")
    token = current_trace_id.set("known-http-trace")
    try:
        assert await wrapped.fn() == {"ok": True}
    finally:
        current_trace_id.reset(token)
    assert [r["event"] for r in events.rows] == [
        "mcp_tool_started",
        "mcp_task_mutation_returned",
        "mcp_task_response_ready",
        "mcp_tool_finished",
    ]
    assert len({r["tool_attempt_id"] for r in events.rows}) == 1
    assert all(r["trace_id"] == "known-http-trace" for r in events.rows)
    assert events.rows[1]["mutation_outcome"] == "application_ok"
    assert events.rows[2]["response_ok"] is True
    assert secret not in str(events.rows)
