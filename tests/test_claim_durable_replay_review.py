"""Idempotent re-claim under a temporary WorkSession must preserve explicit durable ownership."""

from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_durable_claim_not_silently_downgraded_by_session_reclaim(tmp_path):
    import sqlite3

    repo = SqliteRepository(tmp_path / "review.sqlite3")
    await repo.initialize()
    tasks = TaskStore(repo.path)
    await tasks.create_task("review", "one", "Persistent responsibility")
    owner = ClaimOwner.logical_agent("la_one")
    durable = await tasks.claim_owner("review", "one", owner)

    reclaimed = await tasks.claim_owner(
        "review",
        "one",
        owner,
        claim_intent="Observe current work",
        lease={
            "work_session_id": "temporary-window",
            "session_epoch": 2,
            "hard_expires_at": utc_text(utc_now() + timedelta(minutes=5)),
        },
    )

    assert reclaimed["id"] == durable["id"]
    with sqlite3.connect(tasks.path) as db:
        row = db.execute(
            "SELECT c.owner_id,l.work_session_id FROM work_claims c "
            "LEFT JOIN work_claim_leases l ON l.claim_id=c.id WHERE c.id=?",
            (durable["id"],),
        ).fetchone()
    assert row == ("la_one", None)
