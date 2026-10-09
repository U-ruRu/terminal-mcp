"""An idle recovery scanner must never contend with active DB writers."""

import sqlite3
from time import monotonic

import pytest

from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.work_windows import WorkWindowStore


@pytest.mark.asyncio
async def test_empty_recovery_queue_does_not_require_writer_lock(tmp_path):
    """An unrelated writer must not block recovery with no due windows."""
    path = tmp_path / "main.sqlite3"
    await SqliteRepository(path, tmp_path / "out.sqlite3").initialize()
    store = WorkWindowStore(path, authority_node_id="qa-recovery")
    assert await store.claim_window_recovery(limit=4) == []

    with sqlite3.connect(path, isolation_level=None, timeout=0.05) as other_writer:
        other_writer.execute("BEGIN IMMEDIATE")
        try:
            started = monotonic()
            # No due recovery work; scanning must not require BEGIN IMMEDIATE.
            leases = await store.claim_window_recovery(limit=4)
            elapsed = monotonic() - started
            assert leases == []
            assert elapsed < 0.7, f"idle scan blocked by unrelated writer: {elapsed:.3f}s"
        finally:
            other_writer.rollback()
