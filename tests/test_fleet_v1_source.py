"""Fleet source projection rules with journal, metadata and query ports doubled."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from terminal_mcp.fleet.source import FleetSourceService


@pytest.fixture
async def source(tmp_path):
    meta = SimpleNamespace(fleet_id="fleet-a", node_id="node-a", source_stream_generation="gen-a")
    journal = SimpleNamespace(
        high_water_seq=AsyncMock(return_value=7),
        read=AsyncMock(
            return_value={
                "events": [],
                "next_cursor": 7,
                "oldest_seq": 1,
                "gap": False,
                "gap_from_seq": None,
                "gap_to_seq": None,
            }
        ),
    )
    metadata = SimpleNamespace(observe_journal=AsyncMock(return_value=(meta, False)))
    return FleetSourceService(tmp_path / "unused.sqlite3", journal, metadata)


def raw_event(entity_type="logical_agent", payload=None):
    return {
        "seq": 7,
        "event_type": entity_type + ".changed",
        "entity_type": entity_type,
        "entity_id": "logical-a",
        "created_at": "2026-01-01T00:00:01Z",
        "payload": payload or {"slot_revision": 3},
    }


@pytest.mark.asyncio
async def test_source_event_uses_the_node_stream_identity(source):
    source.journal.read.return_value["events"] = [raw_event()]
    source.query_plane.access_identities = AsyncMock(return_value={})

    page = await source.events()

    assert (
        page["events"][0]["event_id"]
        == "379367e66815b2abd505dc048c49cd40c8cde4d8c4d8b51ee2a4314d58edd567"
    )


@pytest.mark.asyncio
async def test_source_replay_adds_the_authoritative_public_name(source):
    source.journal.read.return_value["events"] = [raw_event()]
    source.query_plane.access_identities = AsyncMock(
        return_value={
            "logical-a": {"public_name": "Alpha", "access_generation": 2, "access_status": "active"}
        }
    )

    page = await source.events()

    assert page["events"][0]["payload"]["public_name"] == "Alpha"


@pytest.mark.asyncio
async def test_source_decodes_attachment_presence_arrays(source):
    source.journal.read.return_value["events"] = [
        raw_event(
            "attachment_presence", {"work_scope_json": '["src"]', "details_json": '["inspect"]'}
        )
    ]

    page = await source.events()

    assert page["events"][0]["payload"] == {"work_scope": ["src"], "details": ["inspect"]}


@pytest.mark.asyncio
async def test_source_reset_identifies_a_changed_generation(source):
    page = await source.events(source_stream_generation="old-generation")

    assert page["reset_reason"] == "source_stream_generation_changed"


@pytest.mark.asyncio
async def test_source_reset_identifies_a_retention_gap(source):
    source.journal.read.return_value |= {"gap": True, "gap_from_seq": 1, "gap_to_seq": 6}

    page = await source.events(source_stream_generation="gen-a")

    assert page["reset_reason"] == "retention_gap"


@pytest.mark.asyncio
async def test_current_recovery_preserves_the_requested_barrier(source):
    source.query_plane.recovery_page = AsyncMock(return_value={"entities": []})

    await source.current_recovery("contexts", barrier_source_seq=3)

    assert source.query_plane.recovery_page.await_args.kwargs["barrier"] == 3
