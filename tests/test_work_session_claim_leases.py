from datetime import timedelta

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.persistent_agents import PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.storage.work_windows import WorkWindowStore


@pytest_asyncio.fixture
async def lease_case(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    sessions = WorkWindowStore(repo.path, authority_node_id="home")
    now = utc_now()
    for agent, selector in [("la_one", "ABCD"), ("la_two", "EFGH")]:
        await sessions.create_slot(
            agent,
            agent,
            selector,
            authority_node_id="home",
            initial_arm_duration_seconds=1380,
            now=utc_text(now),
        )
    session = await sessions.start_managed_session(
        "la_one",
        role="executor",
        contract_version=1,
        principal_id="principal",
        auth_generation=1,
        now=now,
    )
    tasks = TaskStore(repo.path)
    await tasks.create_task("lease-test", "task", "claim lease", checkpoint={"keep": "checkpoint"})
    return sessions, tasks, session, now


def lease(snapshot):
    return dict(
        work_session_id=snapshot.session.work_session_id,
        session_epoch=snapshot.session.session_epoch,
        hard_expires_at=snapshot.session.hard_expires_at,
    )


async def stop(sessions, snapshot, now, reason="session_end"):
    args = ("la_one", snapshot.session.work_session_id, snapshot.session.session_epoch)
    await sessions.begin_managed_stop(*args, principal_id="principal", reason=reason, now=now)
    return await sessions.finish_managed_stop(*args, principal_id="principal", now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["session_end", "session_interrupt", "hard_duration"])
async def test_end_interrupt_expiry_release_exact_claim_and_preserve_checkpoint(lease_case, reason):
    sessions, tasks, first, now = lease_case
    await tasks.update_task("lease-test", "task", state="in_progress")
    await tasks.claim_owner(
        "lease-test", "task", ClaimOwner.logical_agent("la_one"), lease=lease(first)
    )
    assert (await tasks.get_task("lease-test", "task"))["state"] == "in_progress"
    when = now + timedelta(seconds=2)
    if reason == "hard_duration":
        when = first.window.hard_expires_at + timedelta(milliseconds=1)
        await sessions.expire_window(
            "la_one", expected_revision=first.window.window_revision, now=when
        )
    await stop(sessions, first, when, reason)
    assert await tasks.active_claims("lease-test", "task") == []
    result = await tasks.get_task("lease-test", "task")
    assert result["state"] == "in_progress"
    assert result["checkpoint"] == {"keep": "checkpoint"}
    revision = result["revision"]
    await stop(sessions, first, when + timedelta(seconds=1), reason)
    assert (await tasks.get_task("lease-test", "task"))["revision"] == revision
    # Another agent may claim immediately after the completed session fence.
    await tasks.claim_owner("lease-test", "task", ClaimOwner.logical_agent("la_two"))
    assert (await tasks.active_claims("lease-test", "task"))[0]["owner_id"] == "la_two"


@pytest.mark.asyncio
async def test_late_cleanup_cannot_release_reclaimed_task_in_successor_session(lease_case):
    sessions, tasks, first, now = lease_case
    await tasks.claim_owner(
        "lease-test", "task", ClaimOwner.logical_agent("la_one"), lease=lease(first)
    )
    await stop(sessions, first, now + timedelta(seconds=1))
    second = await sessions.start_managed_session(
        "la_one",
        role="coordinator",
        contract_version=1,
        principal_id="principal",
        auth_generation=1,
        now=now + timedelta(seconds=2),
    )
    await tasks.claim_owner(
        "lease-test", "task", ClaimOwner.logical_agent("la_one"), lease=lease(second)
    )
    current = await tasks.active_claims("lease-test", "task")
    await stop(sessions, first, now + timedelta(seconds=3))
    assert await tasks.active_claims("lease-test", "task") == current
    assert (
        await sessions.active_session_for_slot("la_one")
    ).work_session_id == second.session.work_session_id


@pytest.mark.asyncio
async def test_explicit_durable_ownership_survives_session_end(lease_case):
    sessions, tasks, first, now = lease_case
    await tasks.update_task("lease-test", "task", state="in_progress")
    await tasks.claim_owner("lease-test", "task", ClaimOwner.logical_agent("la_one"))
    claims = await tasks.active_claims("lease-test", "task")
    await stop(sessions, first, now + timedelta(seconds=1))
    assert await tasks.active_claims("lease-test", "task") == claims
    assert (await tasks.get_task("lease-test", "task"))["state"] == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["epoch", "expired", "wrong_agent", "ended"])
async def test_invalid_session_lease_is_rejected_before_claim_commit(lease_case, invalid):
    sessions, tasks, first, now = lease_case
    fields = lease(first)
    owner = "la_one"
    if invalid == "epoch":
        fields["session_epoch"] += 1
    elif invalid == "expired":
        fields["hard_expires_at"] = utc_text(now - timedelta(seconds=1))
    elif invalid == "wrong_agent":
        owner = "la_two"
    else:
        await stop(sessions, first, now + timedelta(seconds=1))
    with pytest.raises(PersistentStoreError):
        await tasks.claim_owner("lease-test", "task", ClaimOwner.logical_agent(owner), lease=fields)
    assert await tasks.active_claims("lease-test", "task") == []
    assert (await tasks.get_task("lease-test", "task"))["revision"] == 1


@pytest.mark.asyncio
async def test_explicit_release_preserves_ready_and_is_idempotent(lease_case):
    _, tasks, first, _ = lease_case
    owner = ClaimOwner.logical_agent("la_one")
    await tasks.claim_owner("lease-test", "task", owner, lease=lease(first))
    assert await tasks.release_owner_claim_mutation("lease-test", "task", owner, reason="handoff")
    result = await tasks.get_task("lease-test", "task")
    assert result["state"] == "ready"
    assert not await tasks.release_owner_claim_mutation(
        "lease-test", "task", owner, reason="handoff"
    )
    assert (await tasks.get_task("lease-test", "task"))["revision"] == result["revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("audited_managed_claim", [True, False])
async def test_v20_claim_migration_uses_exact_managed_evidence_and_preserves_durable(
    lease_case, audited_managed_claim
):
    import sqlite3

    from terminal_mcp.storage.claim_leases import migrate_existing_claim_leases

    sessions, tasks, first, now = lease_case
    owner = ClaimOwner.logical_agent("la_one")
    await tasks.update_task("lease-test", "task", state="in_progress")
    await tasks.claim_owner("lease-test", "task", owner)  # v20 has no lease.
    if audited_managed_claim:
        await tasks.add_event(
            "lease-test",
            "task",
            "persistent_mutation",
            agent_id="la_one",
            logical_agent_id="la_one",
            work_session_id=first.session.work_session_id,
            session_epoch=first.session.session_epoch,
            payload={"action": "claim"},
        )
    await stop(sessions, first, now + timedelta(seconds=1))
    assert await tasks.active_claims("lease-test", "task")  # old durable behavior
    async with tasks._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await migrate_existing_claim_leases(db, now=utc_text(now + timedelta(seconds=2)))
        await db.commit()
    claims = await tasks.active_claims("lease-test", "task")
    assert bool(claims) is not audited_managed_claim
    item = await tasks.get_task("lease-test", "task")
    assert item["checkpoint"] == {"keep": "checkpoint"}
    assert item["state"] == "in_progress"  # Exact prior manual state survives migration.
    revision = item["revision"]
    async with tasks._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await migrate_existing_claim_leases(db, now=utc_text(now + timedelta(seconds=3)))
        await db.commit()
    assert (await tasks.get_task("lease-test", "task"))["revision"] == revision
    with sqlite3.connect(tasks.path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_cooperative_session_release_preserves_other_owner_until_last_release(lease_case):
    sessions, tasks, first, now = lease_case
    await tasks.update_task("lease-test", "task", cooperative=True, state="in_progress")
    await tasks.claim_owner(
        "lease-test", "task", ClaimOwner.logical_agent("la_one"), lease=lease(first)
    )
    other = await sessions.start_managed_session(
        "la_two",
        role="executor",
        contract_version=1,
        principal_id="other",
        auth_generation=1,
        now=now,
    )
    await tasks.claim_owner(
        "lease-test", "task", ClaimOwner.logical_agent("la_two"), lease=lease(other)
    )
    await stop(sessions, first, now + timedelta(seconds=1))
    item = await tasks.get_task("lease-test", "task")
    assert item["state"] == "in_progress"
    assert (await tasks.active_claims("lease-test", "task"))[0]["owner_id"] == "la_two"
    await tasks.release_owner_claim_mutation(
        "lease-test", "task", ClaimOwner.logical_agent("la_two"), reason="last owner done"
    )
    assert (await tasks.get_task("lease-test", "task"))["state"] == "in_progress"
