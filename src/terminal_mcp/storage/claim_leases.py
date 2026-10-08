"""Exact WorkSession leases over stable LogicalAgent task ownership.

Helpers run inside the caller's write transaction. Durable operator ownership
has no lease row and is deliberately unaffected by session cleanup.
"""

from __future__ import annotations

import json

from terminal_mcp.core.orchestration import parse_utc, utc_now
from terminal_mcp.storage.persistent_agents import PersistentStoreError


async def attach_claim_lease(db, claim_id: int, logical_agent_id: str, lease: dict) -> None:
    session_id = str(lease["work_session_id"])
    epoch = int(lease["session_epoch"])
    expires = str(lease["hard_expires_at"])
    if not session_id or epoch < 1 or utc_now() >= parse_utc(expires):
        raise PersistentStoreError("session_expired")
    fenced = await (
        await db.execute(
            "SELECT 1 FROM work_claim_session_fences WHERE logical_agent_id=? "
            "AND work_session_id=? AND session_epoch=?",
            (logical_agent_id, session_id, epoch),
        )
    ).fetchone()
    if fenced is not None:
        raise PersistentStoreError("session_not_active")
    session = await (
        await db.execute(
            "SELECT logical_agent_id,session_epoch,state,hard_expires_at "
            "FROM logical_agent_work_sessions WHERE work_session_id=?",
            (session_id,),
        )
    ).fetchone()
    # Remote owners are admitted by a verified short-lived Fleet permit. Their
    # local lease deliberately has no FK to an authority-owned WorkSession row.
    if session is not None and (
        session[0] != logical_agent_id
        or int(session[1]) != epoch
        or session[2] != "active"
        or utc_now() >= parse_utc(session[3])
    ):
        raise PersistentStoreError("session_not_active")
    old = await (
        await db.execute(
            "SELECT work_session_id,session_epoch FROM work_claim_leases WHERE claim_id=?",
            (claim_id,),
        )
    ).fetchone()
    if old is not None and (old[0] != session_id or int(old[1]) != epoch):
        raise PersistentStoreError("session_epoch_mismatch")
    await db.execute(
        "INSERT INTO work_claim_leases(claim_id,logical_agent_id,work_session_id,"
        "session_epoch,hard_expires_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(claim_id) DO NOTHING",
        (claim_id, logical_agent_id, session_id, epoch, expires),
    )


async def release_session_claims(
    db, logical_agent_id: str, work_session_id: str, session_epoch: int, *, now: str, reason: str
) -> int:
    rows = await (
        await db.execute(
            "SELECT c.id,c.namespace,c.task_id FROM work_claims c "
            "JOIN work_claim_leases l ON l.claim_id=c.id "
            "WHERE c.released_at IS NULL AND l.logical_agent_id=? "
            "AND l.work_session_id=? AND l.session_epoch=?",
            (logical_agent_id, work_session_id, session_epoch),
        )
    ).fetchall()
    for claim_id, namespace, task_id in rows:
        await db.execute(
            "UPDATE work_claims SET released_at=? WHERE id=? AND released_at IS NULL",
            (now, claim_id),
        )
        await db.execute(
            "INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,"
            "created_at,logical_agent_id,work_session_id,session_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                namespace,
                task_id,
                "claim_released",
                logical_agent_id,
                json.dumps({"reason": reason, "claim_id": claim_id, "scope": "work_session"}),
                now,
                logical_agent_id,
                work_session_id,
                session_epoch,
            ),
        )
    for namespace, task_id in sorted({(row[1], row[2]) for row in rows}):
        await refresh_released_task(db, namespace, task_id, now=now)
    return len(rows)


async def refresh_released_task(db, namespace: str, task_id: str, *, now: str) -> None:
    live = await (
        await db.execute(
            "SELECT 1 FROM work_claims WHERE namespace=? AND task_id=? "
            "AND released_at IS NULL LIMIT 1",
            (namespace, task_id),
        )
    ).fetchone()
    automatic = await (
        await db.execute(
            "SELECT 1 FROM work_claim_auto_state WHERE namespace=? AND task_id=?",
            (namespace, task_id),
        )
    ).fetchone()
    if live is None and automatic is not None:
        await db.execute(
            "UPDATE work_items SET state=CASE WHEN state='in_progress' "
            "THEN 'ready' ELSE state END, "
            "ready_since=CASE WHEN state='in_progress' THEN ? ELSE ready_since END, "
            "state_changed_at=CASE WHEN state='in_progress' THEN ? ELSE state_changed_at END, "
            "revision=revision+1,updated_at=? WHERE namespace=? AND task_id=?",
            (now, now, now, namespace, task_id),
        )
    else:
        # Leave state out of SET: an explicit state assignment clears its provenance.
        await db.execute(
            "UPDATE work_items SET revision=revision+1,updated_at=? "
            "WHERE namespace=? AND task_id=?",
            (now, namespace, task_id),
        )
    leased_owner = await (
        await db.execute(
            "SELECT 1 FROM work_claims c JOIN work_claim_leases l ON l.claim_id=c.id "
            "WHERE c.namespace=? AND c.task_id=? AND c.released_at IS NULL LIMIT 1",
            (namespace, task_id),
        )
    ).fetchone()
    if leased_owner is None:
        await db.execute(
            "DELETE FROM work_claim_auto_state WHERE namespace=? AND task_id=?",
            (namespace, task_id),
        )


async def migrate_existing_claim_leases(db, *, now: str) -> None:
    """Recover only v20 claims with exact managed-session mutation evidence.

    An operator's durable claim without a matching managed claim event remains
    durable. Existing ownership is never rebound to the latest session by name.
    """
    await db.execute(
        "INSERT OR IGNORE INTO work_claim_leases(claim_id,logical_agent_id,work_session_id,"
        "session_epoch,hard_expires_at) "
        "SELECT c.id,c.owner_id,s.work_session_id,s.session_epoch,s.hard_expires_at "
        "FROM work_claims c JOIN logical_agent_work_sessions s ON s.logical_agent_id=c.owner_id "
        "JOIN logical_agent_session_bindings b ON b.work_session_id=s.work_session_id "
        "WHERE c.owner_kind='logical_agent' AND c.released_at IS NULL "
        "AND c.claimed_at>=s.started_at AND c.claimed_at<s.hard_expires_at AND EXISTS("
        "SELECT 1 FROM work_events e WHERE e.namespace=c.namespace AND e.task_id=c.task_id "
        "AND e.logical_agent_id=c.owner_id AND e.work_session_id=s.work_session_id "
        "AND e.session_epoch=s.session_epoch AND e.created_at>=c.claimed_at "
        "AND e.event_type='persistent_mutation' AND json_valid(e.payload_json) "
        "AND json_extract(e.payload_json,'$.action')='claim')"
    )
    ended = await (
        await db.execute(
            "SELECT DISTINCT l.logical_agent_id,l.work_session_id,l.session_epoch "
            "FROM work_claim_leases l JOIN work_claims c ON c.id=l.claim_id "
            "JOIN logical_agent_work_sessions s ON s.work_session_id=l.work_session_id "
            "WHERE c.released_at IS NULL AND s.state IN ('ended','expired','failed','suspended')"
        )
    ).fetchall()
    for agent, session, epoch in ended:
        await release_session_claims(
            db, agent, session, epoch, now=now, reason="schema21_expired_managed_claim_recovery"
        )
