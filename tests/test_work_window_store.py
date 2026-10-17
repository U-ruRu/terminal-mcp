from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.core.provider_identity import ProviderIdentity
from terminal_mcp.core.work_windows import SlotSessionPolicy, WorkWindowError
from terminal_mcp.storage.persistent_agents import PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.work_windows import WorkWindowStore, WorkWindowStoreError

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(tmp_path):
    path = tmp_path / "state.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    value = WorkWindowStore(path, authority_node_id="home")
    for agent, selector in [("la_one", "ABCD"), ("la_two", "EFGH")]:
        await value.create_slot(
            agent,
            agent,
            selector,
            authority_node_id="home",
            initial_arm_duration_seconds=1380,
            now=utc_text(T0),
        )
    return value


async def start(store, agent="la_one", *, now=T0, **kwargs):
    args = dict(role="executor", contract_version=1, principal_id="principal-a", auth_generation=1)
    args.update(kwargs)
    return await store.start_managed_session(agent, now=now, **args)


async def end(store, snapshot, *, now):
    args = (
        snapshot.session.logical_agent_id,
        snapshot.session.work_session_id,
        snapshot.session.session_epoch,
    )
    await store.begin_managed_stop(*args, principal_id="principal-a", now=now)
    return await store.finish_managed_stop(*args, principal_id="principal-a", now=now)


async def test_policy_edit_preserves_open_window(store):
    first = await start(store)

    await store.update_policy(
        "la_one",
        SlotSessionPolicy(default_duration_seconds=3600),
        expected_revision=1,
        principal_id="operator",
    )

    assert await store.current_window("la_one") == first.window


async def test_restart_uses_existing_window_and_new_session_epoch(store):
    first = await start(store)
    await end(store, first, now=T0 + timedelta(seconds=60))

    second = await start(store, now=T0 + timedelta(seconds=120))

    assert second.window == first.window
    assert second.session.work_session_id != first.session.work_session_id
    assert second.session.session_epoch == first.session.session_epoch + 1
    assert second.session.hard_expires_at == first.session.hard_expires_at


async def test_concurrent_starts_share_one_session(store):
    peer = WorkWindowStore(store.path, authority_node_id="home")

    first, second = await asyncio.gather(start(store), start(peer))

    assert first.session == second.session
    assert first.created != second.created


@pytest.mark.parametrize("role,version", [("coordinator", 1), ("executor", 2)])
async def test_active_contract_is_immutable(store, role, version):
    first = await start(store)
    with pytest.raises(WorkWindowError, match="session_contract_conflict"):
        await start(store, role=role, contract_version=version)
    current = await store.session_snapshot("la_one", first.session.work_session_id, 1)
    assert current.binding == first.binding


async def test_other_principal_cannot_reuse_active_session(store):
    await start(store)

    with pytest.raises(WorkWindowStoreError, match="session_principal_mismatch"):
        await start(store, principal_id="principal-b")


async def test_window_mutations_are_atomic_cas_across_store_instances(store):
    first = await start(store)
    peer = WorkWindowStore(store.path, authority_node_id="home")

    async def mutate(instance):
        return await instance.change_window(
            "la_one", expected_revision=1, principal_id="operator", delta_seconds=600, now=T0
        )

    results = await asyncio.gather(mutate(store), mutate(peer), return_exceptions=True)
    assert sum(isinstance(r, WorkWindowStoreError) for r in results) == 1
    failure = next(r for r in results if isinstance(r, WorkWindowStoreError))
    assert failure.code == "revision_conflict"
    current = await store.current_window("la_one")
    assert current.window_revision == 2
    assert current.hard_expires_at == first.window.hard_expires_at + timedelta(seconds=600)
    assert failure.current == current
    session = await store.get_work_session(first.session.work_session_id)
    assert parse_utc(session.hard_expires_at) == current.hard_expires_at


async def test_shortening_revokes_execution_authority_atomically(store):
    first = await start(store)
    now = T0 + timedelta(seconds=900)

    await store.change_window(
        "la_one", expected_revision=1, principal_id="operator", duration_seconds=600, now=now
    )

    session = await store.get_work_session(first.session.work_session_id)
    assert session.state == "stopping"
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await store.assert_session_authority(
            "la_one", session.work_session_id, 1, now=utc_text(now)
        )


async def cooled_window(store):
    first = await start(store)
    await store.update_policy(
        "la_one",
        SlotSessionPolicy(default_duration_seconds=3600),
        expected_revision=1,
        principal_id="operator",
    )
    now = T0 + timedelta(seconds=1380)
    await store.expire_window("la_one", expected_revision=1, now=now)
    await end(store, first, now=now)
    await store.complete_window_fence("la_one", expected_revision=2, now=now)
    return now


async def test_successor_window_uses_latest_policy(store):
    expired_at = await cooled_window(store)

    successor = await start(store, now=expired_at + timedelta(seconds=180))

    assert successor.window.initial_duration_seconds == 3600


async def test_early_start_respects_cooldown(store):
    expired_at = await cooled_window(store)

    with pytest.raises(WorkWindowStoreError, match="window_cooldown"):
        await start(store, now=expired_at + timedelta(seconds=179))


async def test_foreign_authority_cannot_mutate_window(store):
    first = await start(store)
    foreign = WorkWindowStore(store.path, authority_node_id="other-node")

    with pytest.raises(WorkWindowStoreError, match="authority_unavailable"):
        await foreign.change_window(
            "la_one", expected_revision=1, principal_id="operator", delta_seconds=10, now=T0
        )

    assert await store.current_window("la_one") == first.window


async def test_provider_binding_survives_store_reopen(store):
    identity = ProviderIdentity("example", "private-user", "private-conversation")
    await store.bind_provider(identity, "la_one", principal_id="operator", now=T0)
    reopened = WorkWindowStore(store.path, authority_node_id="home")

    assert await reopened.resolve_provider(identity) == "la_one"


async def test_provider_binding_cannot_move_to_another_slot(store):
    identity = ProviderIdentity("example", "private-user", "private-conversation")
    await store.bind_provider(identity, "la_one", principal_id="operator", now=T0)

    with pytest.raises(WorkWindowStoreError, match="identity_binding_conflict"):
        await store.bind_provider(identity, "la_two", principal_id="operator", now=T0)

    assert await store.resolve_provider(identity) == "la_one"


async def test_failed_transaction_rolls_back_window_session_and_audit(store, monkeypatch):
    async def failure(*args, **kwargs):
        raise RuntimeError("simulated audit storage failure")

    monkeypatch.setattr(store, "_audit", failure)
    with pytest.raises(RuntimeError, match="simulated audit storage failure"):
        await start(store)
    assert await store.current_window("la_one") is None
    assert await store.active_session_for_slot("la_one") is None
    assert (await store.get_slot("la_one")).state == "armed"


async def test_session_snapshot_survives_store_reopen(store):
    first = await start(store)
    reopened = WorkWindowStore(store.path, authority_node_id="home")

    snapshot = await reopened.session_snapshot("la_one", first.session.work_session_id, 1)

    assert snapshot.window == first.window
    assert snapshot.session == first.session
    assert snapshot.binding == first.binding


async def test_queued_command_blocks_session_finalization(store):
    first = await start(store)
    repo = SqliteRepository(store.path)
    await repo.create(
        "printf bounded-test",
        queue_id=1,
        agent_id="la_one",
        logical_agent_id="la_one",
        work_session_id=first.session.work_session_id,
        session_epoch=1,
        command_type="persistent_run",
    )
    args = ("la_one", first.session.work_session_id, 1)
    await store.begin_managed_stop(*args, principal_id="principal-a", now=T0)

    with pytest.raises(WorkWindowStoreError, match="session_drain_pending"):
        await store.finish_managed_stop(*args, principal_id="principal-a", now=T0)


@pytest.mark.parametrize("column,value", [("authority_epoch", 2), ("authority_node_id", "other")])
async def test_active_start_rejects_stale_session_authority(store, column, value):
    first = await start(store)
    async with store._transaction("test_fenced_authority") as db:
        await db.execute(
            f"UPDATE logical_agent_work_sessions SET {column}=? WHERE work_session_id=?",
            (value, first.session.work_session_id),
        )
    with pytest.raises(WorkWindowStoreError, match="session_binding_invalid"):
        await start(store)
