from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = b"mesh-native-tests-only-never-production-key"


class Fence:
    def __init__(self):
        self.calls = []

    async def revoke_session(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return []


def actor(role="executor", conversation="a", principal="user"):
    return ActorContext(
        auth_mode="bearer",
        principal_id=principal,
        credential_id="fixture-credential",
        auth_generation=1,
        scopes=frozenset({"terminal:execute", "terminal:read"}),
        provider="openai",
        provider_metadata={"openai/subject": "fixture-user", "openai/session": conversation},
        transport="mcp",
        endpoint_role=role,
    )


@pytest_asyncio.fixture
async def fixture(tmp_path):
    path = tmp_path / "state.db"
    await SqliteRepository(path, tmp_path / "output.db").initialize()
    store = AccessMeshStore(
        path,
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )
    clock = [T0]
    tasks = TaskStore(path)
    fence = Fence()
    app = AccessMeshApplication(
        store, task_store=tasks, execution_fence=fence, clock=lambda: clock[0]
    )
    return SimpleNamespace(
        store=store,
        app=app,
        tasks=tasks,
        fence=fence,
        clock=clock,
        native=PersistentAgentStore(path),
    )


async def issued(f, kind="legacy", **policy):
    return await f.app.issue(
        actor("access"), kind=kind, code="1234", policy=SlotPolicy(10, 5, **policy)
    )


@pytest.mark.asyncio
async def test_native_cycle_survives_restart_and_rearms_without_issuer(fixture):
    f = fixture
    grant = await issued(f)
    attached = await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    assert attached["logical_agent_id"] == grant["logical_agent_id"]
    same = await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)
    assert same["work_session_id"] == attached["work_session_id"]
    restarted = AccessMeshApplication(
        f.store, task_store=f.tasks, execution_fence=f.fence, clock=lambda: f.clock[0]
    )
    assert (await restarted.resolve(actor(), ManagedOperation.COMMAND_RUN))[
        "work_session_id"
    ] == same["work_session_id"]
    f.clock[0] = T0 + timedelta(seconds=11)
    with pytest.raises(AccessMeshError, match="window_cooldown"):
        await restarted.resolve(actor(), ManagedOperation.COMMAND_RUN)
    f.clock[0] = T0 + timedelta(seconds=15)
    next_cycle = await restarted.resolve(actor(), ManagedOperation.COMMAND_RUN)
    assert next_cycle["session_epoch"] == same["session_epoch"] + 1
    assert next_cycle["work_session_id"] != same["work_session_id"]
    native = await f.native.get_work_session(next_cycle["work_session_id"])
    assert native.logical_agent_id == grant["logical_agent_id"]
    assert native.authority_node_id == "firstbyte"
    assert f.fence.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,release", [("legacy", False), ("persistent", False), ("persistent", True)]
)
async def test_expiry_claim_policy_preserves_task_state(fixture, kind, release):
    f = fixture
    grant = await issued(f, kind, release_on_end=release)
    await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    await f.tasks.create_task("mesh", "one", "one", state="in_progress", checkpoint="keep")
    await f.tasks.claim_owner("mesh", "one", ClaimOwner.logical_agent(grant["logical_agent_id"]))
    f.clock[0] = T0 + timedelta(seconds=11)
    await f.app.tick()
    claims = await f.tasks.active_claims("mesh", "one")
    assert bool(claims) is (kind == "persistent" and not release)
    task = await f.tasks.get_task("mesh", "one")
    assert task["state"] == "in_progress" and task["checkpoint"] == "keep"


@pytest.mark.asyncio
async def test_different_metadata_attach_same_agent_other_principal_stays_unbound(fixture):
    f = fixture
    await issued(f)
    a = await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    b = await f.app.attach(
        actor("coordinator", "different"), issuer_node_id=None, access_code="firstbyte:1234"
    )
    assert a["logical_agent_id"] == b["logical_agent_id"]
    with pytest.raises(AccessMeshError, match="session_attach_required"):
        await f.app.resolve(actor(principal="unattached"), ManagedOperation.COMMAND_RUN)


@pytest.mark.asyncio
async def test_delete_durable_cleanup_and_tombstone_after_restart(fixture):
    f = fixture
    grant = await issued(f)
    await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    await f.tasks.create_task("mesh", "one", "one", state="blocked", checkpoint="keep")
    await f.tasks.claim_owner("mesh", "one", ClaimOwner.logical_agent(grant["logical_agent_id"]))
    event = AccessSlotEvent(
        issuer_id="firstbyte",
        slot_id=grant["slot_id"],
        logical_agent_id=grant["logical_agent_id"],
        revision=2,
        event_id="delete",
        kind="SlotDeleted",
    )
    f.store.apply_event(event, authenticated_peer_id="firstbyte")
    assert f.store.pending_cleanup()
    await f.app.tick()
    assert not f.store.pending_cleanup()
    assert not await f.tasks.active_claims("mesh", "one")
    assert (await f.tasks.get_task("mesh", "one"))["state"] == "blocked"
    with pytest.raises(AccessMeshError, match="session_attach_required"):
        await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)


@pytest.mark.asyncio
async def test_snapshot_gap_and_local_issuer_independence(fixture, tmp_path):
    f = fixture
    grant = await issued(f)
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="SlotSuspended", expected_revision=1
    )
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="SlotResumed", expected_revision=2
    )
    path = tmp_path / "bac.db"
    await SqliteRepository(path, tmp_path / "bac-output.db").initialize()
    consumer = AccessMeshStore(
        path,
        local_node_id="bacloud",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )
    snapshot = f.store.snapshot_page(issuer_id="firstbyte")[0]
    assert consumer.apply_snapshot(snapshot, authenticated_peer_id="firstbyte") == "applied"
    assert consumer.apply_snapshot(snapshot, authenticated_peer_id="firstbyte") == "duplicate"
    app = AccessMeshApplication(
        consumer, task_store=TaskStore(path), execution_fence=Fence(), clock=lambda: T0
    )
    result = await app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    assert result["logical_agent_id"] == grant["logical_agent_id"]
    assert result["authority_node_id"] == "bacloud"
    assert result["issuer_node_id"] == "firstbyte"
    assert (await app.issue(actor("access"), code="1234"))["issuer_node_id"] == "bacloud"


@pytest.mark.asyncio
async def test_altered_event_replay_is_rejected(fixture):
    f = fixture
    grant = await issued(f)
    event = f.store.pending_outbox(peer_node_id="bacloud")[0].event
    wire = event.to_wire()
    wire["code_tag"] = f.store.code_tag("firstbyte", "5678")
    with pytest.raises(AccessMeshError, match="access_mesh_event_conflict"):
        f.store.apply_event(AccessSlotEvent.from_wire(wire), authenticated_peer_id="firstbyte")
    assert f.store.slot("firstbyte", grant["slot_id"]).revision == 1


@pytest.mark.asyncio
async def test_deadline_override_changes_current_cycle_only_and_native_fence(fixture):
    f = fixture
    grant = await issued(f)
    attached = await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    result = await f.app.change(
        actor("access"),
        slot_id=grant["slot_id"],
        kind="SessionUpdated",
        expected_revision=1,
        effective_at=T0,
        deadline_at=T0 + timedelta(seconds=20),
    )
    assert result["revision"] == 2
    native = await f.native.get_work_session(attached["work_session_id"])
    assert datetime.fromisoformat(native.hard_expires_at) == T0 + timedelta(seconds=20)
    f.clock[0] = T0 + timedelta(seconds=16)
    current = await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)
    assert current["work_session_id"] == attached["work_session_id"]
    f.clock[0] = T0 + timedelta(seconds=22)
    with pytest.raises(AccessMeshError, match="window_cooldown"):
        await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)
    f.clock[0] = T0 + timedelta(seconds=25)
    following = await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)
    assert following["work_session_id"] != attached["work_session_id"]
    assert following["hard_expires_at"] == (T0 + timedelta(seconds=35)).isoformat(
        timespec="microseconds"
    )
    assert f.store.slot("firstbyte", grant["slot_id"]).policy.duration_seconds == 10


@pytest.mark.asyncio
async def test_status_read_does_not_release_claims_or_materialize_next_cycle(fixture):
    f = fixture
    await issued(f)
    attached = await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    f.clock[0] = T0 + timedelta(seconds=16)
    # The public Agent Observe surface now contains four fields per active
    # named agent. Inspect the pure internal status to verify that reads do
    # not materialize a new local work cycle.
    slot = f.store.attached_slot(f.app.connection_key(actor()))
    status = f.store.observed_identity(slot, now=f.clock[0])
    assert status["session_lifecycle"]["state"] == "active"
    assert "work_session_id" not in status
    public = await f.app.observe(actor("executor"))
    assert public == {"ok": True, "agents": []}
    assert not f.store.pending_cleanup()
    old = await f.native.get_work_session(attached["work_session_id"])
    assert old.state == "active"
    await f.app.tick()
    current = await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)
    assert current["session_epoch"] == attached["session_epoch"] + 1


@pytest.mark.asyncio
async def test_snapshot_rotation_preserves_session_but_missed_stop_fences(fixture, tmp_path):
    f = fixture
    grant = await issued(f)
    path = tmp_path / "snapshot-consumer.db"
    await SqliteRepository(path, tmp_path / "snapshot-out.db").initialize()
    store = AccessMeshStore(
        path,
        local_node_id="bacloud",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )
    consumer = AccessMeshApplication(
        store, task_store=TaskStore(path), execution_fence=Fence(), clock=lambda: T0
    )
    store.apply_snapshot(
        f.store.snapshot_page(issuer_id="firstbyte")[0], authenticated_peer_id="firstbyte"
    )
    initial = await consumer.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="AccessCodeRotated", expected_revision=1
    )
    store.apply_snapshot(
        f.store.snapshot_page(issuer_id="firstbyte")[0], authenticated_peer_id="firstbyte"
    )
    assert not store.pending_cleanup()
    assert (await consumer.resolve(actor(), ManagedOperation.COMMAND_RUN))[
        "work_session_id"
    ] == initial["work_session_id"]
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="SlotSuspended", expected_revision=2
    )
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="SlotResumed", expected_revision=3
    )
    store.apply_snapshot(
        f.store.snapshot_page(issuer_id="firstbyte")[0], authenticated_peer_id="firstbyte"
    )
    assert store.pending_cleanup()
    assert (await consumer.resolve(actor(), ManagedOperation.COMMAND_RUN))[
        "session_epoch"
    ] == initial["session_epoch"] + 1
