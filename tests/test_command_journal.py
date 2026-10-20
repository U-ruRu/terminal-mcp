"""Journal keyset storage and cursor projection, with neighboring ports isolated."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.commands import CommandApplication
from terminal_mcp.core.read_contract import decode_cursor, encode_cursor
from terminal_mcp.storage.access_mesh import AccessMeshStore

JOURNAL_SCOPE = {"kind": "command.journal", "node_id": "node", "order": "sequence_desc"}
ACTOR = ActorContext(endpoint_role="executor", transport="mcp")


@pytest.fixture
def store(tmp_path):
    result = AccessMeshStore(
        tmp_path / "journal.db",
        local_node_id="node",
        trusted_issuers=frozenset({"node"}),
        proof_key=b"j" * 32,
    )
    # Only columns consumed by this storage query; no app/auth/runtime wiring.
    with result._connect() as db:
        db.executescript("""
            CREATE TABLE commands(
                hash TEXT PRIMARY KEY, enqueued_at TEXT, started_at TEXT,
                finished_at TEXT, status TEXT, queue_id INTEGER, exit_code INTEGER
            );
            CREATE TABLE command_agent_attribution(
                command_hash TEXT PRIMARY KEY, created_at TEXT,
                agent_id TEXT, logical_agent_id TEXT
            );
            CREATE TABLE logical_agents(logical_agent_id TEXT PRIMARY KEY, display_name TEXT);
        """)
    result.initialize_command_journal()
    return result


def _append(store, number):
    with store._connect() as db:
        db.execute(
            "INSERT INTO commands(hash,enqueued_at,status,exit_code) VALUES(?,?,?,?)",
            (f"{number:08x}", "2026-10-20T08:00:00Z", "completed", 0),
        )


@pytest.mark.parametrize("count", [0, 1, 5])
def test_journal_store_starts_at_newest_command(store, count):
    for number in range(1, count + 1):
        _append(store, number)

    page = store.command_journal(limit=2)

    assert [row["cmd_hash"] for row in page] == [
        f"{number:08x}" for number in range(count, max(0, count - 2), -1)
    ]


@pytest.mark.parametrize("mutation", [None, "append", "delete_anchor"])
def test_journal_store_continues_strictly_before_anchor(store, mutation):
    for number in range(1, 7):
        _append(store, number)
    first = store.command_journal(limit=2)
    boundary = first[-1]["sequence"]
    if mutation == "append":
        _append(store, 7)
    elif mutation == "delete_anchor":
        with store._connect() as db:
            db.execute("DELETE FROM commands WHERE hash=?", (first[-1]["cmd_hash"],))

    page = store.command_journal(before=boundary, limit=2)

    assert [row["cmd_hash"] for row in page] == ["00000004", "00000003"]


def _application(rows):
    journal = Mock(return_value=rows)
    store = SimpleNamespace(local_node_id="node", command_journal=journal)
    gate = SimpleNamespace(access_mesh=SimpleNamespace(store=store))
    return CommandApplication(SimpleNamespace(), gate), journal


def _rows(*sequences):
    return [{"sequence": number, "cmd_hash": f"{number:08x}"} for number in sequences]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, ""])
async def test_journal_first_page_has_no_storage_boundary(cursor):
    app, journal = _application(_rows(9, 8, 7))

    result = await app.journal(ACTOR, limit=2, cursor=cursor)

    journal.assert_called_once_with(before=None, limit=3)
    assert result["commands"] == [{"cmd_hash": "00000009"}, {"cmd_hash": "00000008"}]
    assert decode_cursor(result["next_cursor"], JOURNAL_SCOPE) == 8


@pytest.mark.asyncio
async def test_journal_cursor_passes_exclusive_sequence_to_storage():
    app, journal = _application(_rows(7, 6, 5))

    result = await app.journal(ACTOR, limit=2, cursor=encode_cursor(8, JOURNAL_SCOPE))

    journal.assert_called_once_with(before=8, limit=3)
    assert result["commands"] == [{"cmd_hash": "00000007"}, {"cmd_hash": "00000006"}]
    assert decode_cursor(result["next_cursor"], JOURNAL_SCOPE) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("sequences", [(), (1,), (2, 1)])
async def test_journal_terminal_page_has_no_cursor(sequences):
    app, _ = _application(_rows(*sequences))

    result = await app.journal(ACTOR, limit=2)

    assert result["ok"] is True
    assert result["next_cursor"] is None
    assert len(result["commands"]) == len(sequences)


@pytest.mark.asyncio
async def test_journal_rejects_previous_ascending_cursor():
    app, journal = _application([])
    old_scope = {"kind": "command.journal", "node_id": "node"}

    result = await app.journal(ACTOR, cursor=encode_cursor(2, old_scope))

    assert result["ok"] is False
    assert result["code"] == "invalid_cursor"
    journal.assert_not_called()
