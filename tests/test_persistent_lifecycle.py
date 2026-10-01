import asyncio
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import parse_utc
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.persistent_lifecycle import (
    PersistentLifecycleCoordinator,
    PersistentLifecycleError,
)
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


def admission(principal="client-1"):
    return VerifiedAdmissionContext(
        principal_id=principal,
        credential_id=f"oauth:{principal}",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )


class Fence:
    def __init__(self):
        self.session_blockers = []
        self.slot_blockers = []
        self.claim_blockers = []
        self.revocations = []

    async def revoke_session(self, logical_agent_id, work_session_id, session_epoch, *, reason):
        self.revocations.append((logical_agent_id, work_session_id, session_epoch, reason))
        return list(self.session_blockers)

    async def blockers_for_slot(self, logical_agent_id):
        return list(self.slot_blockers)

    async def blockers_for_claim(self, namespace, task_id, logical_agent_id):
        return list(self.claim_blockers)


async def setup(tmp_path, *, enabled=True, duration=120, rearm=180, fence=None):
    repo = SqliteRepository(tmp_path / "main.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    lifecycle = PersistentLifecycleCoordinator(
        store,
        enabled=enabled,
        authority_node_id="node-a",
        session_duration_seconds=duration,
        rearm_delay_seconds=rearm,
        execution_fence=fence,
    )
    return repo, store, TaskStore(repo.path), lifecycle


@pytest.mark.asyncio
async def test_feature_gate_and_authenticated_admission_are_fail_closed(tmp_path):
    _, _, _, disabled = await setup(tmp_path, enabled=False)
    with pytest.raises(PersistentLifecycleError, match="policy_incompatible"):
        await disabled.list_slots()

    _, _, _, lifecycle = await setup(tmp_path / "enabled")
    with pytest.raises(PersistentLifecycleError, match="persistent_auth_required"):
        await lifecycle.create_slot("Alpha")

    read_only = VerifiedAdmissionContext(
        principal_id="reader",
        credential_id="oauth:reader",
        scopes=frozenset({"terminal:read"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )
    with pytest.raises(PersistentLifecycleError, match="persistent_scope_required"):
        await lifecycle.create_slot("Alpha", admission=read_only)


@pytest.mark.asyncio
async def test_play_and_session_start_capture_duration_and_one_concurrent_winner(tmp_path):
    _, store, _, lifecycle = await setup(tmp_path, duration=90)
    created = await lifecycle.create_slot("Alpha", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    rev = created["slot"]["slot_revision"]
    armed = await lifecycle.play(logical_agent_id, expected_revision=rev, admission=admission())
    assert armed["arm"]["captured_duration_seconds"] == 90
    selector = created["selector"]["selector"]
    armed_revision = armed["slot"]["slot_revision"]

    async def attempt():
        try:
            return await lifecycle.session_start(
                selector, expected_revision=armed_revision, admission=admission()
            )
        except PersistentLifecycleError as exc:
            return exc.code

    first, second = await asyncio.gather(attempt(), attempt())
    results = [first, second]
    winners = [item for item in results if isinstance(item, dict)]
    losers = [item for item in results if isinstance(item, str)]
    assert len(winners) == 1
    assert losers[0] in {"revision_conflict", "session_already_active"}
    session = winners[0]["work_session"]
    assert (
        parse_utc(session["hard_expires_at"]) - parse_utc(session["started_at"])
    ).total_seconds() == 90
    assert (await store.get_slot(logical_agent_id)).state == "active"


@pytest.mark.asyncio
async def test_session_authority_is_exact_and_bound_to_verified_principal(tmp_path):
    _, _, _, lifecycle = await setup(tmp_path)
    created = await lifecycle.create_slot("Alpha", admission=admission("owner"))
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await lifecycle.play(
        logical_agent_id, expected_revision=1, admission=admission("owner")
    )
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=admission("owner"),
    )
    session = started["work_session"]
    authorized = await lifecycle.authorize_session(
        logical_agent_id,
        session["work_session_id"],
        session["session_epoch"],
        admission=admission("owner"),
    )
    assert authorized.work_session_id == session["work_session_id"]
    with pytest.raises(PersistentLifecycleError, match="persistent_auth_required"):
        await lifecycle.authorize_session(
            logical_agent_id,
            session["work_session_id"],
            session["session_epoch"],
            admission=admission("other"),
        )


@pytest.mark.asyncio
async def test_suspend_fences_execution_but_retains_persistent_claims(tmp_path):
    fence = Fence()
    _, store, tasks, lifecycle = await setup(tmp_path, fence=fence)
    created = await lifecycle.create_slot("Alpha", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    await tasks.create_task("ns", "T-1", "Task")
    await tasks.claim_owner("ns", "T-1", ClaimOwner.logical_agent(logical_agent_id))
    armed = await lifecycle.play(logical_agent_id, expected_revision=1, admission=admission())
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=admission(),
    )
    fence.session_blockers = [{"kind": "running_command", "command_hash": "deadbeef"}]
    stopping = await lifecycle.suspend(
        logical_agent_id, expected_revision=started["slot"]["slot_revision"], admission=admission()
    )
    assert stopping["stopping"] is True
    assert stopping["slot"]["state"] == "stopping"
    assert len(await tasks.claims_for_owner(ClaimOwner.logical_agent(logical_agent_id))) == 1
    fence.session_blockers = []
    current = await store.active_session_for_slot(logical_agent_id)
    finished = await lifecycle._stop_active_session(
        logical_agent_id, current, reason="suspend", terminal_state="suspended"
    )
    assert finished["slot"]["state"] == "suspended"
    assert len(await tasks.claims_for_owner(ClaimOwner.logical_agent(logical_agent_id))) == 1


@pytest.mark.asyncio
async def test_hard_duration_reconciliation_does_not_release_claims(tmp_path):
    _, store, tasks, lifecycle = await setup(tmp_path, duration=30)
    created = await lifecycle.create_slot("Alpha", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    await tasks.create_task("ns", "T-1", "Task")
    await tasks.claim_owner("ns", "T-1", ClaimOwner.logical_agent(logical_agent_id))
    armed = await lifecycle.play(logical_agent_id, expected_revision=1, admission=admission())
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=admission(),
    )
    after = parse_utc(started["work_session"]["hard_expires_at"]) + timedelta(seconds=1)
    reconciled = await lifecycle.reconcile_expired(now=after)
    assert len(reconciled) == 1
    session = await store.get_work_session(started["work_session"]["work_session_id"])
    assert session.state == "expired"
    assert (await store.get_slot(logical_agent_id)).state == "suspended"
    pending = await store.pending_rearm(logical_agent_id)
    assert pending is not None
    assert (
        parse_utc(pending["rearm_at"]) - parse_utc(session.ended_at)
    ).total_seconds() == 180
    assert len(await tasks.claims_for_owner(ClaimOwner.logical_agent(logical_agent_id))) == 1


@pytest.mark.asyncio
async def test_session_end_schedules_durable_auto_rearm_without_starting_session(tmp_path):
    _, store, _, lifecycle = await setup(tmp_path, duration=30, rearm=5)
    created = await lifecycle.create_slot("Alpha", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await lifecycle.play(logical_agent_id, expected_revision=1, admission=admission())
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=admission(),
    )
    session = started["work_session"]
    ended = await lifecycle.session_end(
        logical_agent_id,
        session["work_session_id"],
        session["session_epoch"],
        admission=admission(),
    )
    assert ended["slot"]["state"] == "suspended"
    pending = await store.pending_rearm(logical_agent_id)
    assert pending is not None
    assert (
        parse_utc(pending["rearm_at"]) - parse_utc(ended["work_session"]["ended_at"])
    ).total_seconds() == 5

    before = parse_utc(pending["rearm_at"]) - timedelta(seconds=1)
    assert await lifecycle.reconcile_rearms(now=before) == []
    assert (await store.get_slot(logical_agent_id)).state == "suspended"

    restarted = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="node-a",
        session_duration_seconds=30,
        rearm_delay_seconds=5,
    )
    after = parse_utc(pending["rearm_at"]) + timedelta(seconds=1)
    reconciled = await restarted.reconcile_rearms(now=after)
    assert len(reconciled) == 1
    assert reconciled[0]["auto_rearmed"] is True
    assert (await store.get_slot(logical_agent_id)).state == "armed"
    assert await store.active_session_for_slot(logical_agent_id) is None
    assert await restarted.reconcile_rearms(now=after) == []


@pytest.mark.asyncio
async def test_manual_suspend_cancels_pending_auto_rearm(tmp_path):
    _, store, _, lifecycle = await setup(tmp_path, duration=30, rearm=5)
    created = await lifecycle.create_slot("Alpha", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await lifecycle.play(logical_agent_id, expected_revision=1, admission=admission())
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=admission(),
    )
    ended = await lifecycle.session_end(
        logical_agent_id,
        started["work_session_id"],
        started["session_epoch"],
        admission=admission(),
    )
    pending = await store.pending_rearm(logical_agent_id)
    assert pending is not None
    suspended = await lifecycle.suspend(
        logical_agent_id,
        expected_revision=ended["slot"]["slot_revision"],
        admission=admission(),
    )
    assert suspended["slot"]["state"] == "suspended"
    assert await store.pending_rearm(logical_agent_id) is None
    after = parse_utc(pending["rearm_at"]) + timedelta(seconds=1)
    assert await lifecycle.reconcile_rearms(now=after) == []
    assert (await store.get_slot(logical_agent_id)).state == "suspended"


@pytest.mark.asyncio
async def test_delete_and_reassign_fail_closed_on_execution_blockers(tmp_path):
    fence = Fence()
    _, _, tasks, lifecycle = await setup(tmp_path, fence=fence)
    source = await lifecycle.create_slot("Alpha", admission=admission())
    target = await lifecycle.create_slot("Bravo", admission=admission())
    source_id = source["slot"]["logical_agent_id"]
    target_id = target["slot"]["logical_agent_id"]
    await tasks.create_task("ns", "T-1", "Task")
    await tasks.claim_owner("ns", "T-1", ClaimOwner.logical_agent(source_id))
    fence.claim_blockers = [{"kind": "queued_command", "command_hash": "deadbeef"}]
    with pytest.raises(PersistentLifecycleError) as exc:
        await lifecycle.claim_reassign("ns", "T-1", source_id, target_id, admission=admission())
    assert exc.value.code == "reassign_blocked"
    fence.claim_blockers = []
    await lifecycle.claim_reassign("ns", "T-1", source_id, target_id, admission=admission())
    assert await tasks.claims_for_owner(ClaimOwner.logical_agent(source_id)) == []
    assert len(await tasks.claims_for_owner(ClaimOwner.logical_agent(target_id))) == 1
    fence.slot_blockers = [{"kind": "detached_command", "command_hash": "cafebabe"}]
    with pytest.raises(PersistentLifecycleError) as exc:
        await lifecycle.delete(source_id, expected_revision=1, admission=admission())
    assert exc.value.code == "delete_blocked"
    fence.slot_blockers = []
    deleted = await lifecycle.delete(source_id, expected_revision=1, admission=admission())
    assert deleted["slot"]["state"] == "deleted"


@pytest.mark.asyncio
async def test_foreign_node_cannot_mutate_or_authorize_home_slot(tmp_path):
    _, store, _, home = await setup(tmp_path, duration=30)
    created = await home.create_slot("Alpha", admission=admission("owner"))
    logical_agent_id = created["slot"]["logical_agent_id"]
    foreign = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="node-b",
        session_duration_seconds=30,
    )

    with pytest.raises(PersistentLifecycleError) as exc:
        await foreign.play(
            logical_agent_id,
            expected_revision=created["slot"]["slot_revision"],
            admission=admission("owner"),
        )
    assert exc.value.code == "authority_unavailable"

    armed = await home.play(
        logical_agent_id,
        expected_revision=created["slot"]["slot_revision"],
        admission=admission("owner"),
    )
    started = await home.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=admission("owner"),
    )
    session = started["work_session"]
    with pytest.raises(PersistentLifecycleError) as exc:
        await foreign.authorize_session(
            logical_agent_id,
            session["work_session_id"],
            session["session_epoch"],
            admission=admission("owner"),
        )
    assert exc.value.code == "authority_unavailable"

    after = parse_utc(session["hard_expires_at"]) + timedelta(seconds=1)
    assert await foreign.reconcile_expired(now=after) == []
    assert (await store.get_work_session(session["work_session_id"])).state == "active"
