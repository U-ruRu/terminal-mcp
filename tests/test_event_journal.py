import json

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.events import EventJournalStore, compact_payload
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_event_journal_monotonic_cursor_retention_and_explicit_gap(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = EventJournalStore(path, max_rows=5)
    await store.initialize()

    seqs = [
        await store.append(
            "test.changed",
            "test",
            str(index),
            payload={"index": index},
            created_at=f"2026-01-01T00:00:0{index}Z",
        )
        for index in range(1, 8)
    ]

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 7

    page = await store.read(since=1, limit=10)
    assert page["gap"] is True
    assert page["gap_from_seq"] == 2
    assert page["gap_to_seq"] == 2
    assert page["oldest_seq"] == 3
    assert page["high_water_seq"] == 7
    assert [event["seq"] for event in page["events"]] == [3, 4, 5, 6, 7]
    assert page["next_cursor"] == 7

    contiguous = await store.read(since=2, limit=2)
    assert contiguous["gap"] is False
    assert [event["seq"] for event in contiguous["events"]] == [3, 4]
    assert contiguous["next_cursor"] == 4


@pytest.mark.asyncio
async def test_shared_mutation_boundaries_emit_compact_events(tmp_path):
    repo = SqliteRepository(tmp_path / "terminal.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    journal = EventJournalStore(repo.path)
    agents = AgentStore(repo.path)
    tasks = TaskStore(repo.path)
    contexts = ContextStore(repo.path)

    await agents.create_session(
        "Quebec-TEST",
        "Event test",
        "Initial intent",
        ["repo:test"],
        ["one"],
        1,
        "2026-01-01T00:00:00Z",
    )
    await agents.update_coordinate(
        "Quebec-TEST",
        "Changed intent",
        1,
        "2026-01-01T00:00:01Z",
    )
    await agents.activity(
        "Quebec-TEST",
        "run",
        "2026-01-01T00:00:02Z",
        command_hash="deadbeef",
    )

    await agents.create_session(
        "Romeo-TEST",
        "Receiver",
        "Read",
        [],
        ["one"],
        1,
        "2026-01-01T00:00:03Z",
    )
    await agents.create_message(
        "a1b2c3d4",
        "Quebec-TEST",
        "Romeo",
        "secret message body must not enter event journal",
        "2026-01-01T00:00:04Z",
        ["Romeo-TEST"],
        require_reply=True,
        alert=False,
    )
    await agents.acknowledge_message(
        "a1b2c3d4",
        "Romeo-TEST",
        "2026-01-01T00:00:05Z",
    )

    context = await contexts.create(
        "Safe summary",
        "private context content must not enter event journal",
        True,
    )
    await contexts.update(context["id"], summary="Updated summary")
    await contexts.delete(context["id"])

    await tasks.create_task_mutation(
        "events",
        "TASK-1",
        "Event task",
        event_agent_id="Quebec-TEST",
        event_payload={"state": "ready"},
        now="2026-01-01T00:00:06Z",
    )
    await tasks.claim(
        "events",
        "TASK-1",
        "Quebec-TEST",
        claim_intent="implement",
        now="2026-01-01T00:00:07Z",
    )
    await tasks.add_event(
        "events",
        "TASK-1",
        "review",
        agent_id="Quebec-TEST",
        payload={"verdict": "pass"},
        now="2026-01-01T00:00:08Z",
    )

    command = await repo.create(
        "printf super-secret-output",
        status="queued",
        cmd_hash="cafebabe",
        agent_id="Quebec-TEST",
        command_type="run",
        command_preview="printf super-secret-output",
        queue_id=1,
    )
    assert command.cmd_hash == "cafebabe"
    claimed = await repo.claim_next(1)
    assert claimed is not None
    assert await repo.finish_running("cafebabe", "completed", 0) is True
    await agents.finish("Quebec-TEST", now="2026-01-01T00:00:09Z")

    page = await journal.read(since=0, limit=1000)
    event_types = [event["event_type"] for event in page["events"]]

    assert "agent.started" in event_types
    assert "agent.intent" in event_types
    assert "agent.activity" in event_types
    assert "agent.ended" in event_types
    assert "message.created" in event_types
    assert "message.receipt" in event_types
    assert "context.created" in event_types
    assert "context.updated" in event_types
    assert "context.deleted" in event_types
    assert "task.created" in event_types
    assert "task.claim" in event_types
    assert "task.review" in event_types
    assert "command.created" in event_types
    assert event_types.count("command.status") >= 2

    serialized = json.dumps(page, ensure_ascii=False)
    assert "secret message body" not in serialized
    assert "private context content" not in serialized
    assert "super-secret-output" not in serialized
    assert page["events"] == sorted(page["events"], key=lambda event: event["seq"])



@pytest.mark.asyncio
async def test_task_journal_payload_is_whitelisted(tmp_path):
    repo = SqliteRepository(tmp_path / "terminal.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    tasks = TaskStore(repo.path)
    journal = EventJournalStore(repo.path)

    await tasks.create_task_mutation(
        "safe",
        "TASK-1",
        "Task",
        event_agent_id="Quebec-TEST",
        event_payload={"state": "ready"},
        now="2026-01-01T00:00:00Z",
    )
    await tasks.add_event(
        "safe",
        "TASK-1",
        "updated",
        agent_id="Quebec-TEST",
        payload={
            "state": "blocked",
            "lane": "implementation",
            "priority": 3,
            "fields": ["state", "checkpoint", "description"],
            "candidate_ref": "candidate-abc",
            "checkpoint": {"token": "checkpoint-secret"},
            "result": {"token": "result-secret"},
            "description": "description-secret",
            "next_action": "next-action-secret",
            "text": "comment-secret",
            "evidence": {"token": "evidence-secret"},
            "warnings": [{"code": "safe-code", "message": "warning-secret"}],
        },
        now="2026-01-01T00:00:01Z",
    )

    page = await journal.read(since=0, limit=100)
    event = next(item for item in page["events"] if item["event_type"] == "task.updated")
    assert event["payload"]["state"] == "blocked"
    assert event["payload"]["lane"] == "implementation"
    assert event["payload"]["priority"] == 3
    assert event["payload"]["candidate_ref"] == "candidate-abc"
    assert event["payload"]["fields"] == ["state", "checkpoint", "description"]

    serialized = json.dumps(event["payload"], ensure_ascii=False)
    for secret in (
        "checkpoint-secret",
        "result-secret",
        "description-secret",
        "next-action-secret",
        "comment-secret",
        "evidence-secret",
        "warning-secret",
    ):
        assert secret not in serialized
    for forbidden_key in (
        "checkpoint",
        "result",
        "description",
        "next_action",
        "text",
        "evidence",
        "warnings",
    ):
        assert forbidden_key not in event["payload"]


class _HealthTerminal:
    queue_workers = 1

    def __init__(self):
        self.ok = True

    async def health(self):
        return {
            "ok": self.ok,
            "user": "root",
            "uid": 0,
            "gid": 0,
            "cwd": "/",
            "privilege": "root",
            "shell": "/bin/bash",
            "terminal_user": "root",
            "scheduler": "numbered_fifo",
            "parallelism": 1,
            "queue_size": 0,
            "running_commands": [],
            "queues": [{"queue_id": 1, "running": None, "queued": 0}],
            "worker_health": {"1": self.ok},
        }


@pytest.mark.asyncio
async def test_health_journal_emits_only_material_changes(tmp_path):
    repo = SqliteRepository(tmp_path / "terminal.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = _HealthTerminal()
    service = TerminalService(repo, terminal, 100)
    journal = EventJournalStore(repo.path)

    first = await service.health("none")
    second = await service.health("none")
    terminal.ok = False
    third = await service.health("none")

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is False

    page = await journal.read(since=0, limit=100)
    health = [event for event in page["events"] if event["event_type"] == "health.changed"]
    assert len(health) == 2
    assert health[0]["payload"]["ok"] is True
    assert health[1]["payload"]["ok"] is False


def test_compact_payload_drops_large_terminal_fields():
    encoded = compact_payload(
        {
            "command": "rm -rf /secret",
            "stdout": "x" * 10000,
            "nested": {"content": "private", "value": "y" * 1000},
        }
    )
    payload = json.loads(encoded)

    assert "command" not in payload
    assert "stdout" not in payload
    assert "content" not in payload["nested"]
    assert len(payload["nested"]["value"]) <= 512
