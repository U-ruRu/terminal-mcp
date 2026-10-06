from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.core.provider_identity import ProviderIdentity
from terminal_mcp.core.work_windows import (
    SlotSessionPolicy,
    WindowLifecycle,
    WindowPhase,
    WorkWindowError,
)
from terminal_mcp.storage.persistent_agents import PersistentStoreError
from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository
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


async def test_policy_is_per_slot_and_existing_window_is_snapshot(store):
    first = await start(store)
    old = await store.policy("la_one")
    changed = await store.update_policy(
        "la_one",
        SlotSessionPolicy(default_duration_seconds=3600),
        expected_revision=old.revision,
        principal_id="operator",
    )
    assert changed.revision == 2
    assert (await store.policy("la_two")).policy.default_duration_seconds == 1380
    assert (await store.current_window("la_one")) == first.window
    with pytest.raises(WorkWindowStoreError, match="revision_conflict") as err:
        await store.update_policy(
            "la_one", SlotSessionPolicy(), expected_revision=1, principal_id="operator"
        )
    assert err.value.current == changed


async def test_end_start_keeps_deadline_but_advances_session_and_epoch(store):
    first = await start(store)
    stopped = await end(store, first, now=T0 + timedelta(seconds=60))
    assert stopped.state == "ended"
    second = await start(store, now=T0 + timedelta(seconds=120), role="coordinator")
    assert second.window == first.window
    assert second.session.work_session_id != first.session.work_session_id
    assert second.session.session_epoch == first.session.session_epoch + 1
    assert second.session.hard_expires_at == first.session.hard_expires_at
    assert second.binding.role == "coordinator"
    # Retrying an old end is idempotent and cannot stop the new session.
    await end(store, first, now=T0 + timedelta(seconds=121))
    active = await store.assert_session_authority(
        "la_one",
        second.session.work_session_id,
        second.session.session_epoch,
        now=utc_text(T0 + timedelta(seconds=122)),
    )
    assert active.state == "active"


async def test_start_is_idempotent_across_concurrent_connections(store):
    second_store = WorkWindowStore(store.path, authority_node_id="home")
    results = await asyncio.gather(*(start(store if i % 2 else second_store) for i in range(12)))
    assert len({r.window.work_window_id for r in results}) == 1
    assert len({r.session.work_session_id for r in results}) == 1
    assert {r.session.session_epoch for r in results} == {1}
    assert sum(r.created for r in results) == 1


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
    with pytest.raises(WorkWindowStoreError, match="auth_generation_mismatch"):
        await start(store, auth_generation=2)


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


async def test_extend_recomputes_draining_to_active_without_session_restart(store):
    first = await start(store)
    now = T0 + timedelta(seconds=1320)
    assert first.window.phase(now) is WindowPhase.DRAINING
    change = await store.change_window(
        "la_one", expected_revision=1, principal_id="operator", delta_seconds=1200, now=now
    )
    assert change.state is WindowPhase.ACTIVE
    same = await start(store, now=now)
    assert same.session.work_session_id == first.session.work_session_id
    assert same.session.session_epoch == first.session.session_epoch
    assert same.session.hard_expires_at == utc_text(change.current.hard_expires_at)


async def test_shorten_atomically_revokes_existing_execution_authority(store):
    first = await start(store)
    now = T0 + timedelta(seconds=900)
    change = await store.change_window(
        "la_one", expected_revision=1, principal_id="operator", duration_seconds=600, now=now
    )
    assert change.state is WindowPhase.EXPIRED
    assert change.current.expired_at == now
    assert change.current.rearm_at == now + timedelta(seconds=180)
    session = await store.get_work_session(first.session.work_session_id)
    assert session.state == "stopping"
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await store.assert_session_authority(
            "la_one", session.work_session_id, 1, now=utc_text(now)
        )
    with pytest.raises(WorkWindowError, match="window_expired"):
        await store.change_window(
            "la_one", expected_revision=2, principal_id="operator", delta_seconds=3600, now=now
        )


async def test_elapsed_window_cannot_be_restarted_without_fencing(store):
    first = await start(store)
    now = T0 + timedelta(seconds=2000)
    with pytest.raises(WorkWindowStoreError, match="session_expired"):
        await start(store, now=now)
    expired = await store.expire_window("la_one", expected_revision=1, now=now)
    assert expired.expired_at == T0 + timedelta(seconds=1380)
    with pytest.raises(WorkWindowStoreError, match="session_drain_pending"):
        await store.complete_window_fence("la_one", expected_revision=2, now=now)
    await end(store, first, now=now)
    cooldown = await store.complete_window_fence("la_one", expected_revision=2, now=now)
    assert cooldown.lifecycle is WindowLifecycle.COOLDOWN
    assert cooldown.rearm_at == T0 + timedelta(seconds=1560)
    # Simulated process restart reuses durable cooldown and captured deadline.
    restarted = WorkWindowStore(store.path, authority_node_id="home")
    successor = await start(restarted, now=now)
    assert successor.window.work_window_id != first.window.work_window_id
    assert successor.session.session_epoch == 2
    assert successor.window.hard_expires_at == now + timedelta(seconds=1380)


async def test_cooldown_blocks_early_start_and_next_window_uses_new_policy(store):
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
    with pytest.raises(WorkWindowStoreError, match="window_cooldown"):
        await start(store, now=now + timedelta(seconds=179))
    successor = await start(store, now=now + timedelta(seconds=180))
    assert successor.window.initial_duration_seconds == 3600
    assert first.window.initial_duration_seconds == 1380


async def test_finalization_requires_prior_revocation_and_no_durable_commands(store, monkeypatch):
    first = await start(store)
    args = ("la_one", first.session.work_session_id, 1)
    with pytest.raises(WorkWindowStoreError, match="session_not_stopping"):
        await store.finish_managed_stop(*args, principal_id="principal-a", now=T0)
    await store.begin_managed_stop(*args, principal_id="principal-a", now=T0)

    async def blocked(*args, **kwargs):
        return True

    monkeypatch.setattr(store, "_command_blocked", blocked)
    with pytest.raises(WorkWindowStoreError, match="session_drain_pending"):
        await store.finish_managed_stop(*args, principal_id="principal-a", now=T0)
    assert (await store.get_work_session(first.session.work_session_id)).state == "stopping"


async def test_foreign_authority_cannot_mutate_identity_policy_or_windows(store):
    first = await start(store)
    peer = WorkWindowStore(store.path, authority_node_id="other-node")
    with pytest.raises(WorkWindowStoreError, match="authority_unavailable"):
        await peer.policy("la_one")
    with pytest.raises(WorkWindowStoreError, match="authority_unavailable"):
        await peer.change_window(
            "la_one", expected_revision=1, principal_id="operator", delta_seconds=10, now=T0
        )
    with pytest.raises(WorkWindowStoreError, match="authority_unavailable"):
        await peer.bind_provider(
            ProviderIdentity("example", "user", "chat"), "la_one", principal_id="operator"
        )
    assert await store.current_window("la_one") == first.window


async def test_provider_binding_idempotent_durable_and_contains_no_raw_identity(store):
    identity = ProviderIdentity("example", "private-user", "private-conversation")
    assert await store.resolve_provider(identity) is None
    await store.bind_provider(identity, "la_one", principal_id="operator", now=T0)
    await store.bind_provider(identity, "la_one", principal_id="operator", now=T0)
    again = WorkWindowStore(store.path, authority_node_id="home")
    assert await again.resolve_provider(identity) == "la_one"
    # Provider evidence can be resolved on a non-authority Fleet node so that
    # transport can route to the canonical authority. Binding still requires home.
    peer = WorkWindowStore(store.path, authority_node_id="other-node")
    assert await peer.resolve_provider(identity) == "la_one"
    assert await again.resolve_provider(replace(identity, conversation="different")) is None
    with pytest.raises(WorkWindowStoreError, match="identity_binding_conflict"):
        await again.bind_provider(identity, "la_two", principal_id="operator")
    async with store._connect("test") as db:
        rows = await (await db.execute("SELECT * FROM logical_agent_provider_bindings")).fetchall()
        audit = await (
            await db.execute(
                "SELECT payload_json FROM persistent_agent_audit WHERE event_type='provider_bound'"
            )
        ).fetchall()
    assert len(rows) == len(audit) == 1
    persisted = json.dumps([rows, audit])
    assert "private-user" not in persisted and "private-conversation" not in persisted


async def test_failed_transaction_rolls_back_window_session_and_audit(store, monkeypatch):
    async def failure(*args, **kwargs):
        raise RuntimeError("simulated audit storage failure")

    monkeypatch.setattr(store, "_audit", failure)
    with pytest.raises(RuntimeError, match="simulated audit storage failure"):
        await start(store)
    assert await store.current_window("la_one") is None
    assert await store.active_session_for_slot("la_one") is None
    assert (await store.get_slot("la_one")).state == "armed"


async def test_cancelled_transaction_does_not_leave_partial_state(store, monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "_audit", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await start(store)
    assert await store.current_window("la_one") is None
    assert await store.active_session_for_slot("la_one") is None


@pytest.mark.parametrize("bad_revision", [0, -1, True, 1.0, "1"])
async def test_revision_types_are_strict(store, bad_revision):
    with pytest.raises(WorkWindowStoreError, match="revision_invalid"):
        await store.update_policy(
            "la_one", SlotSessionPolicy(), expected_revision=bad_revision, principal_id="operator"
        )


async def test_snapshot_survives_restart_with_immutable_contract(store):
    first = await start(store)
    reopened = WorkWindowStore(store.path, authority_node_id="home")
    snapshot = await reopened.session_snapshot("la_one", first.session.work_session_id, 1)
    assert snapshot.window == first.window
    assert snapshot.session == first.session
    assert snapshot.binding == first.binding
    with pytest.raises(WorkWindowStoreError, match="session_not_found"):
        await reopened.session_snapshot("la_two", first.session.work_session_id, 1)


async def test_legacy_session_adoption_preserves_exact_ids_deadline_and_provenance(store):
    _, session = await store.start_session(
        selector="ABCD",
        work_session_id="legacy-session",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id="node-b",
        session_duration_seconds=1380,
        now=utc_text(T0),
    )
    with pytest.raises(WorkWindowStoreError, match="session_migration_required"):
        await start(store)
    before = asdict(session)
    migrated = await store.adopt_legacy_session(
        "la_one", "legacy-session", 1, principal_id="operator", now=T0
    )
    again = await store.adopt_legacy_session(
        "la_one", "legacy-session", 1, principal_id="operator", now=T0
    )
    assert migrated.session == again.session == session
    assert asdict(await store.get_work_session("legacy-session")) == before
    assert migrated.binding.role == "legacy" and migrated.binding.contract_version == 1
    assert migrated.window.hard_expires_at == parse_utc(session.hard_expires_at)
    assert migrated.window == again.window


async def test_migration_preserves_fractional_legacy_remaining_budget(store):
    _, session = await store.start_session(
        selector="ABCD",
        work_session_id="legacy-fractional",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id=None,
        session_duration_seconds=1380,
        now=utc_text(T0),
    )
    deadline = T0 + timedelta(seconds=120, milliseconds=125)
    async with store._transaction("test_legacy_resume") as db:
        await db.execute(
            "UPDATE logical_agent_work_sessions SET hard_expires_at=? WHERE work_session_id=?",
            (utc_text(deadline), session.work_session_id),
        )
    migrated = await store.adopt_legacy_session(
        "la_one", session.work_session_id, 1, principal_id="operator", now=T0
    )
    assert migrated.window.hard_expires_at == deadline
    assert parse_utc(migrated.session.started_at) == T0
    assert migrated.window.opened_at <= T0


async def test_schema_installer_is_additive_idempotent_and_preserves_state(store):
    first = await start(store)
    await store.bind_provider(
        ProviderIdentity("example", "u", "c"), "la_one", principal_id="operator"
    )
    repo = SqliteRepository(store.path)
    await repo.initialize()
    restored = await store.session_snapshot("la_one", first.session.work_session_id, 1)
    assert restored.window == first.window
    async with store._connect("test_schema") as db:
        version = await (await db.execute("PRAGMA user_version")).fetchone()
        integrity = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
    assert version[0] == SCHEMA_VERSION
    assert not integrity


async def test_durable_commands_prevent_session_finalization_until_cancelled(store):
    first = await start(store)
    repo = SqliteRepository(store.path)
    command = await repo.create(
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
    assert await repo.cancel_queued(command.cmd_hash)
    final = await store.finish_managed_stop(*args, principal_id="principal-a", now=T0)
    assert final.state == "ended"


async def test_slot_claim_survives_end_start_unchanged(store):
    first = await start(store)
    stamp = utc_text(T0)
    async with store._transaction("test_claim_fixture") as db:
        await db.execute(
            "INSERT INTO work_items(namespace,task_id,title,lane,priority,state,"
            "state_changed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "example",
                "task1",
                "durable claim",
                "implementation",
                0,
                "in_progress",
                stamp,
                stamp,
                stamp,
            ),
        )
        await db.execute(
            "INSERT INTO work_claims(namespace,task_id,agent_id,claimed_at,owner_kind,owner_id) "
            "VALUES('example','task1','la_one',?,'logical_agent','la_one')",
            (stamp,),
        )
        before = await (await db.execute("SELECT * FROM work_claims")).fetchall()
    await end(store, first, now=T0 + timedelta(seconds=10))
    second = await start(store, now=T0 + timedelta(seconds=20))
    assert second.session.session_epoch == 2
    async with store._connect("test_claim_unchanged") as db:
        after = await (await db.execute("SELECT * FROM work_claims")).fetchall()
    assert before == after


async def test_sqlite_backup_restores_provider_policy_window_and_session(store, tmp_path):
    import aiosqlite

    first = await start(store)
    identity = ProviderIdentity("example", "user", "chat")
    await store.bind_provider(identity, "la_one", principal_id="operator")
    await store.update_policy(
        "la_one",
        SlotSessionPolicy(default_duration_seconds=2400),
        expected_revision=1,
        principal_id="operator",
    )
    target = tmp_path / "snapshot.sqlite3"
    async with store._connect("backup_test") as source, aiosqlite.connect(target) as destination:
        await source.backup(destination)
    restored = WorkWindowStore(target, authority_node_id="home")
    assert await restored.resolve_provider(identity) == "la_one"
    assert (await restored.policy("la_one")).policy.default_duration_seconds == 2400
    snapshot = await restored.session_snapshot("la_one", first.session.work_session_id, 1)
    assert snapshot.window == first.window
    assert snapshot.session == first.session
    assert snapshot.binding == first.binding


async def test_legacy_adoption_keeps_source_policy_for_next_windows(store):
    await store.start_session(
        selector="ABCD",
        work_session_id="legacy-policy",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id=None,
        session_duration_seconds=1500,
        now=utc_text(T0),
    )
    actual = SlotSessionPolicy(
        default_duration_seconds=1500,
        warning_before_expiry_seconds=100,
        draining_before_expiry_seconds=50,
        rearm_after_seconds=30,
    )
    result = await store.adopt_legacy_session(
        "la_one", "legacy-policy", 1, principal_id="operator", policy=actual, now=T0
    )
    assert (await store.policy("la_one")).policy == actual
    assert result.window.hard_expires_at == T0 + timedelta(seconds=1500)
    assert result.window.warning_before_expiry_seconds == 100


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


async def test_v19_upgrade_is_additive_and_does_not_implicitly_adopt_legacy_sessions(store):
    import sqlite3

    _, legacy = await store.start_session(
        selector="ABCD",
        work_session_id="existing-v19",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id="original-node",
        session_duration_seconds=1380,
        now=utc_text(T0),
    )
    # Reproduce a true v19 database: none of the new managed tables exists.
    with sqlite3.connect(store.path) as db:
        before_slots = db.execute(
            "SELECT * FROM logical_agents ORDER BY logical_agent_id"
        ).fetchall()
        before_sessions = db.execute("SELECT * FROM logical_agent_work_sessions").fetchall()
        for table in (
            "logical_agent_session_bindings",
            "logical_agent_work_windows",
            "logical_agent_session_policies",
            "logical_agent_provider_bindings",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=19")
    await SqliteRepository(store.path).initialize()
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT * FROM logical_agents ORDER BY logical_agent_id").fetchall()
            == before_slots
        )
        assert db.execute("SELECT * FROM logical_agent_work_sessions").fetchall() == before_sessions
        assert db.execute("SELECT COUNT(*) FROM logical_agent_work_windows").fetchone()[0] == 0
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    # Legacy identity remains intact; switching to managed mode is a distinct
    # explicit operation, not a side effect of starting the new binary.
    assert await store.get_work_session("existing-v19") == legacy
    adopted = await store.adopt_legacy_session(
        "la_one", "existing-v19", 1, principal_id="operator", now=T0
    )
    assert adopted.session == legacy


async def test_legacy_start_cannot_reset_a_managed_window_after_end(store):
    first = await start(store)
    await end(store, first, now=T0 + timedelta(seconds=10))
    slot = await store.get_slot("la_one")
    with pytest.raises(PersistentStoreError, match="managed_session_required"):
        await store.start_session(
            selector="ABCD",
            work_session_id="illegal-legacy-restart",
            expected_revision=slot.slot_revision,
            principal_id="principal-a",
            auth_generation=1,
            authority_node_id="home",
            origin_instance_id=None,
            session_duration_seconds=1380,
            now=utc_text(T0 + timedelta(seconds=100)),
        )
    assert await store.current_window("la_one") == first.window
    assert await store.get_work_session("illegal-legacy-restart") is None


async def test_legacy_finalizers_cannot_overwrite_managed_rearm_semantics(store):
    first = await start(store)
    with pytest.raises(PersistentStoreError, match="managed_session_required"):
        await store.begin_session_stop(
            "la_one", first.session.work_session_id, 1, reason="session_end", now=utc_text(T0)
        )
    with pytest.raises(PersistentStoreError, match="managed_session_required"):
        await store.finalize_session_stop(
            "la_one",
            first.session.work_session_id,
            1,
            reason="session_end",
            rearm_delay_seconds=180,
            now=utc_text(T0),
        )
    assert (await store.get_work_session(first.session.work_session_id)).state == "active"
    assert await store.pending_rearm("la_one") is None


async def test_legacy_reconciler_leaves_managed_expiry_to_managed_gate(store):
    from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator

    first = await start(store)
    coordinator = PersistentLifecycleCoordinator(
        store, authority_node_id="home", enabled=True, session_duration_seconds=1380
    )
    assert await coordinator.reconcile_expired(now=T0 + timedelta(seconds=2000)) == []
    assert (await store.get_work_session(first.session.work_session_id)).state == "active"
    # Existing command authority still checks the exact hard deadline, so this
    # is NOT authority to execute after expiry while reconciliation is pending.
    with pytest.raises(PersistentStoreError, match="session_expired"):
        await store.assert_session_authority(
            "la_one", first.session.work_session_id, 1, now=utc_text(T0 + timedelta(seconds=2000))
        )


async def test_legacy_adoption_recovers_original_window_start_across_resumed_sessions(store):
    _, first = await store.start_session(
        selector="ABCD",
        work_session_id="original",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id=None,
        session_duration_seconds=1380,
        now=utc_text(T0),
    )
    stamp = utc_text(T0 + timedelta(seconds=60))
    await store.begin_session_stop(
        "la_one", first.work_session_id, 1, reason="session_end", now=stamp
    )
    slot, _ = await store.finalize_session_stop(
        "la_one", first.work_session_id, 1, reason="session_end", rearm_delay_seconds=180, now=stamp
    )
    _, resumed = await store.start_session(
        selector="ABCD",
        work_session_id="resumed",
        expected_revision=slot.slot_revision,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id=None,
        session_duration_seconds=1380,
        now=utc_text(T0 + timedelta(seconds=120)),
    )
    assert resumed.hard_expires_at == first.hard_expires_at
    adopted = await store.adopt_legacy_session(
        "la_one", resumed.work_session_id, 2, principal_id="operator", now=T0
    )
    assert adopted.window.opened_at == T0
    assert adopted.window.initial_duration_seconds == 1380
    assert adopted.session.started_at == utc_text(T0 + timedelta(seconds=120))
    assert adopted.session.session_epoch == 2
