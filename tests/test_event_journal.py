import json

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.events import EventJournalStore, compact_payload
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_event_journal_reports_gap_after_retention(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = EventJournalStore(path, max_rows=5)
    await store.initialize()

    [
        await store.append(
            "test.changed",
            "test",
            str(index),
            payload={"index": index},
            created_at=f"2026-01-01T00:00:0{index}Z",
        )
        for index in range(1, 8)
    ]

    page = await store.read(since=1, limit=10)
    assert page["gap"] is True
    assert page["gap_from_seq"] == 2
    assert page["gap_to_seq"] == 2
    assert page["oldest_seq"] == 3
    assert page["high_water_seq"] == 7
    assert [event["seq"] for event in page["events"]] == [3, 4, 5, 6, 7]
    assert page["next_cursor"] == 7


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
async def test_health_journal_records_changed_worker_health(tmp_path):
    repo = SqliteRepository(tmp_path / "terminal.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = _HealthTerminal()
    service = TerminalService(repo, terminal, 100)
    journal = EventJournalStore(repo.path)

    await service.health("none")
    cursor = await journal.high_water_seq()
    terminal.ok = False

    await service.health("none")

    page = await journal.read(since=cursor, limit=100)
    assert len(page["events"]) == 1
    assert page["events"][0]["event_type"] == "health.changed"
    assert page["events"][0]["payload"]["ok"] is False


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
