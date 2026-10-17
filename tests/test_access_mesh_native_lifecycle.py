from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import SlotPolicy
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


async def issued(f, kind="temporary", **policy):
    return await f.app.issue(
        actor("access"), kind=kind, code="1234", policy=SlotPolicy(10, 5, **policy)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,release", [("persistent", False), ("persistent", True)])
async def test_expiry_claim_policy_preserves_task_state(fixture, kind, release):
    f = fixture
    grant = await issued(f, kind, release_on_end=release)
    await f.app.attach(actor(), session_number="1234")
    await f.tasks.create_task("mesh", "one", "one", state="in_progress", checkpoint="keep")
    await f.tasks.claim_owner("mesh", "one", ClaimOwner.logical_agent(grant["logical_agent_id"]))
    f.clock[0] = T0 + timedelta(seconds=11)
    await f.app.tick()
    claims = await f.tasks.active_claims("mesh", "one")
    assert bool(claims) is (kind == "persistent" and not release)
    task = await f.tasks.get_task("mesh", "one")
    assert task["state"] == "in_progress" and task["checkpoint"] == "keep"


@pytest.mark.asyncio
async def test_deadline_override_updates_native_session_deadline(fixture):
    f = fixture
    grant = await issued(f, "persistent")
    await f.app.attach(actor(), session_number="1234")
    attached = f.store.observed_identity(
        f.store.attached_slot(f.app.connection_key(actor())), now=f.clock[0]
    )
    await f.app.change(
        actor("access"),
        slot_id=grant["slot_id"],
        kind="SessionUpdated",
        expected_revision=1,
        effective_at=T0,
        deadline_at=T0 + timedelta(seconds=20),
    )
    native = await f.native.get_work_session(attached["work_session_id"])
    assert datetime.fromisoformat(native.hard_expires_at) == T0 + timedelta(seconds=20)


@pytest.mark.asyncio
async def test_expired_status_read_preserves_native_session(fixture):
    f = fixture
    await issued(f, "persistent")
    await f.app.attach(actor(), session_number="1234")
    attached = f.store.observed_identity(
        f.store.attached_slot(f.app.connection_key(actor())), now=f.clock[0]
    )
    f.clock[0] = T0 + timedelta(seconds=16)
    slot = f.store.attached_slot(f.app.connection_key(actor()))
    status = f.store.observed_identity(slot, now=f.clock[0])
    assert status["session_lifecycle"]["state"] == "expired"
    assert (await f.native.get_work_session(attached["work_session_id"])).state == "active"


@pytest.mark.asyncio
async def test_snapshot_of_missed_stop_schedules_native_fence(fixture, tmp_path):
    f = fixture
    grant = await issued(f, "persistent")
    path = tmp_path / "consumer.db"
    await SqliteRepository(path, tmp_path / "consumer-out.db").initialize()
    store = AccessMeshStore(
        path,
        local_node_id="bacloud",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )
    store.apply_snapshot(
        f.store.snapshot_page(issuer_id="firstbyte")[0], authenticated_peer_id="firstbyte"
    )
    slot = store.slot("firstbyte", grant["slot_id"])
    initial = store.local_identity(slot, now=T0)
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="SlotSuspended", expected_revision=1
    )
    await f.app.change(
        actor("access"), slot_id=grant["slot_id"], kind="SlotResumed", expected_revision=2
    )
    store.apply_snapshot(
        f.store.snapshot_page(issuer_id="firstbyte")[0], authenticated_peer_id="firstbyte"
    )
    (cleanup,) = store.pending_cleanup()
    assert cleanup["work_session_id"] == initial["work_session_id"]
