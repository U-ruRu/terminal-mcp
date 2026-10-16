"""LIVE022: local routing, acknowledgment semantics and bounded receipt observability."""

from types import SimpleNamespace

import pytest
from test_access_mesh_messages_v2 import _public_name, actor
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
async def test_route_errors_are_safe_field_specific_and_do_not_commit(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    named = _public_name(fb.mesh.store.slots["carol"])
    for fields, path, reason in (
        ({"namespace": "team"}, "task_id", "missing_required"),
        ({"task_id": "item"}, "namespace", "missing_required"),
        ({"namespace": "team", "task_id": "item", "target": named}, "target", "conflicting_fields"),
    ):
        result = await fb.message(actor("alice"), text="do not deliver", **fields)
        assert result["ok"] is False and result["code"] == "input_validation_failed"
        assert result["path"] == path and result["reason"] == reason
        assert result["outcome"] == "not_committed" and result["retry"] == "repair"
        issues = result["details"]["validation_errors"]
        assert len(issues) == 1 and issues[0]["path"] == path
        assert len(issues[0]["description"]) < 160
        assert _count(fb) == 0


@pytest.mark.asyncio
async def test_both_valid_local_routes_and_broadcast_task_equivalence(mesh_pair, monkeypatch):
    fb = mesh_pair.nodes["firstbyte"]
    named = _public_name(fb.mesh.store.slots["carol"])

    async def owners(namespace, task_id):
        assert (namespace, task_id) == ("team", "item")
        return [{"owner_id": "carol"}]

    monkeypatch.setattr(fb.mesh.task_store, "active_claims", owners)
    direct = await fb.message(actor("alice"), text="direct", target=named)
    task = await fb.message(actor("alice"), text="task", namespace="team", task_id="item")
    task_broadcast = await fb.message(
        actor("alice"),
        text="task-broadcast",
        namespace="team",
        task_id="item",
        target="broadcast",
    )
    for item in (direct, task, task_broadcast):
        assert item["ok"] and item["delivered_to"] == [named]
        assert item["message_hash"]
    assert len({x["message_hash"] for x in (direct, task, task_broadcast)}) == 3
    assert _count(fb) == 3


@pytest.mark.asyncio
async def test_notify_ack_alert_and_reply_receipts_without_duplicate_messages(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    named = _public_name(fb.mesh.store.slots["carol"])
    msgs = {}
    for mode in ("notify", "ack", "alert"):
        sent = await fb.message(actor("alice"), text=f"mode={mode}", target=named, mode=mode)
        assert sent["ok"] and sent["mode"] == mode
        msgs[mode] = sent["message_hash"]
        assert _row(fb, msgs[mode]) == {"first_seen_at": None, "read_at": None, "replied_at": None}
    assert _count(fb) == 3

    inbox = await fb.message(actor("carol"), detail="summary")
    by_hash = {m["message_id"]: m for m in inbox["messages"]}
    assert by_hash[msgs["notify"]]["acknowledged"] is True
    assert by_hash[msgs["ack"]]["acknowledged"] is False
    assert by_hash[msgs["alert"]]["acknowledged"] is False
    for msg in msgs.values():
        assert _row(fb, msg)["first_seen_at"] is not None
    assert _row(fb, msgs["notify"])["read_at"] is not None
    assert _row(fb, msgs["ack"])["read_at"] is None
    assert _row(fb, msgs["alert"])["read_at"] is None

    # ACK may be repeated, including on notify, but it must not add messages.
    for mode in ("notify", "ack", "alert"):
        first = await fb.message(actor("carol"), message_hash=msgs[mode])
        stamp = _row(fb, msgs[mode])["read_at"]
        second = await fb.message(actor("carol"), message_hash=msgs[mode])
        assert first["ok"] and second["ok"] and _row(fb, msgs[mode])["read_at"] == stamp
        assert stamp is not None
        assert _count(fb) == 3
    assert _row(fb, msgs["alert"])["replied_at"] is None
    reply = await fb.message(actor("carol"), message_hash=msgs["alert"], text="completed")
    assert reply["ok"] and _row(fb, msgs["alert"])["replied_at"] is not None
    assert _count(fb) == 4  # exactly one intentional new reply message
    await fb.message(actor("carol"), message_hash=msgs["alert"])
    assert _count(fb) == 4


@pytest.mark.asyncio
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
    assert events.rows[1]["duration_ms"] >= 0
    assert events.rows[2]["duration_ms"] >= events.rows[1]["duration_ms"]
    assert secret not in str(events.rows)
