"""Transaction helpers; the application never imports this SQLite adapter."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.work_windows import WindowLifecycle, WorkWindow


async def install_window_recovery_schema(db: aiosqlite.Connection) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS logical_agent_window_recovery(
        work_window_id TEXT PRIMARY KEY REFERENCES logical_agent_work_windows(work_window_id)
            ON DELETE CASCADE,
        next_check_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        lease_token TEXT, lease_expires_at TEXT, last_error_code TEXT,
        updated_at TEXT NOT NULL)""")
    await db.execute("""CREATE INDEX IF NOT EXISTS ix_managed_recovery_due
        ON logical_agent_window_recovery(next_check_at, work_window_id)""")
    # Additive bootstrapping for an earlier schema-20 core candidate. No identity,
    # window budget or live lease is changed. SQL backfill does not materialize
    # the complete managed history in process memory.
    await db.execute("""INSERT OR IGNORE INTO logical_agent_window_recovery
        (work_window_id,next_check_at,updated_at)
        SELECT w.work_window_id,
            CASE WHEN json_extract(w.snapshot_json,'$.lifecycle')='expired'
                THEN json_extract(w.snapshot_json,'$.expired_at')
                ELSE strftime('%Y-%m-%dT%H:%M:%fZ',
                    json_extract(w.snapshot_json,'$.opened_at'),
                    '+' || json_extract(w.snapshot_json,'$.effective_duration_seconds')
                    || ' seconds') END,
            strftime('%Y-%m-%dT%H:%M:%fZ','now')
        FROM logical_agent_work_windows w
        WHERE w.superseded_at IS NULL
          AND json_extract(w.snapshot_json,'$.lifecycle') IN ('open','expired')""")


async def schedule_window_recovery(
    db: aiosqlite.Connection,
    window: WorkWindow,
    *,
    now: datetime | None = None,
    due_at: datetime | None = None,
) -> None:
    if window.lifecycle is WindowLifecycle.COOLDOWN:
        await db.execute(
            "DELETE FROM logical_agent_window_recovery WHERE work_window_id=?",
            (window.work_window_id,),
        )
        return
    due = due_at or (
        window.expired_at if window.lifecycle is WindowLifecycle.EXPIRED else window.hard_expires_at
    )
    await db.execute(
        """INSERT INTO logical_agent_window_recovery
        (work_window_id,next_check_at,updated_at) VALUES(?,?,?)
        ON CONFLICT(work_window_id) DO UPDATE SET
            next_check_at=excluded.next_check_at,updated_at=excluded.updated_at""",
        (window.work_window_id, utc_text(due), utc_text(now)),
    )
    # Preserve an in-flight lease across domain revisions. Its completion reads
    # the CURRENT snapshot and cannot overwrite a newer operator deadline.
