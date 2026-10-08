"""Real TaskApplication/backend/SQLite claims, isolated from transport admission.

Grant admission is covered by native mesh tests. This module verifies the public
application's dispatch policy and the backend's actual ownership/receipt writes.
"""

import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.task_requests import TaskClaimRequest
from terminal_mcp.application.tasks import TaskApplication
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.storage.work_windows import WorkWindowStore


class Backend(PersistentBackend):
    def __init__(self, persistent_store, tasks, work_session, *, mesh_enabled, mode):
        self.lifecycle = SimpleNamespace(
            store=persistent_store, operation_guard=self._operation_guard
        )
        self.service = SimpleNamespace(events=None, access_mesh=object() if mesh_enabled else None)
        self.task_coordinator = TaskCoordinator(tasks)
        self.task_store = tasks
        self.fleet_bridge = None
        self.work_session = work_session
        self.mode = mode
        self.seen_claim_dispatch = []

    @asynccontextmanager
    async def _operation_guard(self, *args, **kwargs):
        yield

    async def _execution_authority(self, *args, **kwargs):
        return self.work_session, None

    async def _access_get(self, *args, **kwargs):
        return {"slot_kind": self.mode}

    async def _audit(self, *args, **kwargs):
        pass

    async def task(self, **kwargs):
        self.seen_claim_dispatch.append(kwargs.get("session_scoped_claim"))
        return await super().task(**kwargs)


@pytest_asyncio.fixture
async def case(tmp_path):
    repo = SqliteRepository(tmp_path / "policy.db", tmp_path / "output.db")
    await repo.initialize()
    now = utc_now()
    persistent = WorkWindowStore(repo.path, authority_node_id="firstbyte")
    await persistent.create_slot(
        "la_policy",
        "Policy owner",
        "PX19",
        authority_node_id="firstbyte",
        initial_arm_duration_seconds=1380,
        now=utc_text(now),
    )
    snapshot = await persistent.start_managed_session(
        "la_policy",
        role="executor",
        contract_version=1,
        principal_id="operator",
        auth_generation=1,
        now=now,
    )
    tasks = TaskStore(repo.path)
    sessions = persistent
    work_session = snapshot.session
    await tasks.create_task("policy", "task", "Original", state="ready")
    return SimpleNamespace(
        tasks=tasks, sessions=sessions, persistent=persistent, session=work_session, now=now
    )


def backend(case, *, mesh_enabled=True, mode="persistent"):
    return Backend(case.persistent, case.tasks, case.session, mesh_enabled=mesh_enabled, mode=mode)


def claim_args(case, **extra):
    return {
        "logical_agent_id": case.session.logical_agent_id,
        "work_session_id": case.session.work_session_id,
        "session_epoch": case.session.session_epoch,
        "action": "claim",
        "namespace": "policy",
        "task_id": "task",
        "claim_intent": "verify public claim",
        **extra,
    }


def lease_count(case):
    with sqlite3.connect(case.tasks.path) as db:
        return db.execute("SELECT COUNT(*) FROM work_claim_leases").fetchone()[0]


class Gate:
    def __init__(self, case, *, mesh_enabled=False):
        self.case = case
        self.mesh_enabled = mesh_enabled

    async def identity(self, *args):
        session = self.case.session
        identity = {
            "logical_agent_id": session.logical_agent_id,
            "work_session_id": session.work_session_id,
            "session_epoch": session.session_epoch,
            "session_lifecycle": {
                "state": "active",
                "remaining_seconds": 900,
                "hard_expires_at": session.hard_expires_at,
            },
        }
        if self.mesh_enabled:
            # Real Access Mesh attachment has the issuer+slot tuple.
            identity["issuer_node_id"] = "firstbyte"
            identity["slot_id"] = "qa-lease-policy"
        return identity, None


@pytest.mark.asyncio
@pytest.mark.parametrize("mesh_enabled", [False, True])
@pytest.mark.parametrize("mode", ["legacy", "persistent"])
async def test_application_claim_uses_native_mesh_cleanup_not_legacy_window_lease(
    case, mesh_enabled, mode
):
    runtime = backend(case, mesh_enabled=mesh_enabled, mode=mode)
    service = SimpleNamespace(persistent=runtime, access_mesh=runtime.service.access_mesh)
    app = TaskApplication(service, Gate(case, mesh_enabled=mesh_enabled))
    result = await app.task(
        ActorContext(transport="mcp", endpoint_role="executor", request_id=None),
        TaskClaimRequest(action="claim", namespace="policy", task_id="task", claim_intent="owned"),
    )
    assert result["ok"], result
    assert runtime.seen_claim_dispatch == [not mesh_enabled]
    assert lease_count(case) == (0 if mesh_enabled else 1)
    assert len(await case.tasks.active_claims("policy", "task")) == 1
    assert result["task"]["state"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "persistent"])
async def test_backend_mesh_authority_cannot_be_overridden_by_old_lease_hint(case, mode):
    runtime = backend(case, mode=mode)
    result = await runtime.task(**claim_args(case, session_scoped_claim=True))
    assert result["ok"] and lease_count(case) == 0
    # Old window expiry cleanup is not the authority for a mesh slot. Its actual
    # policy can retain persistent claims or extend/rearm a legacy deadline.
    after_old_deadline = utc_text(utc_now() + timedelta(days=2))
    assert await case.tasks.stale_leased_claims(now=after_old_deadline) == []
    assert len(await case.tasks.active_claims("policy", "task")) == 1


@pytest.mark.asyncio
async def test_claim_never_reads_a_list_after_its_transaction_commits(case, monkeypatch):
    runtime = backend(case)

    async def unavailable(*args, **kwargs):
        pytest.fail("post-commit list read must not be called")

    monkeypatch.setattr(runtime.task_coordinator, "list", unavailable)
    result = await runtime.task(**claim_args(case))
    assert result["ok"], result
    assert result["task"]["revision"] == 1
    assert result["task"]["state"] == "ready"
    assert len(await case.tasks.active_claims("policy", "task")) == 1


@pytest.mark.asyncio
async def test_claim_receipt_retains_committed_revision_under_a_later_writer(case, monkeypatch):
    runtime = backend(case)
    original = runtime.task_coordinator.mutate

    async def later_writer(*args, **kwargs):
        result = await original(*args, **kwargs)
        await case.tasks.update_task("policy", "task", title="Later writer", state="blocked")
        return result

    monkeypatch.setattr(runtime.task_coordinator, "mutate", later_writer)
    result = await runtime.task(**claim_args(case))
    assert result["ok"] and result["task"]["revision"] == 1
    assert result["task"]["state"] == "ready"
    assert result["task"]["title"] == "Original"
    current = await case.tasks.get_task("policy", "task")
    assert current["revision"] == 2 and current["state"] == "blocked"


@pytest.mark.asyncio
async def test_claim_receipt_replay_after_restart_ignores_newer_canonical_task(case):
    runtime = backend(case)
    arguments = claim_args(case, idempotency_key="stable-claim-request")
    first = await runtime.task(**arguments)
    assert first["ok"], first
    await case.tasks.update_task("policy", "task", title="Newer", state="deferred")
    restarted = backend(case)
    replay = await restarted.task(**arguments)
    assert replay["ok"] and replay["task"]["title"] == "Newer"
    assert replay["task"]["revision"] == 2
    assert (await case.tasks.get_task("policy", "task"))["revision"] == 2
    assert len(await case.tasks.active_claims("policy", "task")) == 1


@pytest.mark.asyncio
async def test_mesh_claim_never_consults_legacy_window_or_access_lookup(case, monkeypatch):
    runtime = backend(case)

    async def obsolete_lookup(*args, **kwargs):
        pytest.fail("Mesh claim reached a legacy window/access lookup")

    monkeypatch.setattr(runtime, "_access_get", obsolete_lookup)
    monkeypatch.setattr(runtime.lifecycle.store, "has_managed_window", obsolete_lookup)
    result = await runtime.task(**claim_args(case, session_scoped_claim=True))
    assert result["ok"] and lease_count(case) == 0
