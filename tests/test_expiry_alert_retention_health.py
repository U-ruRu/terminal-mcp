"""Regression coverage for expiry-alert retention and full-sweep health recovery."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair_fixture

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.storage.access_mesh_messages import MESSAGE_TTL, MeshMessagingError

mesh_pair = mesh_pair_fixture


def expired_identity(local):
    slot = local.mesh.store.slots["carol"]
    slot.expires_at = local.mesh.now - timedelta(seconds=1)
    identity = local.mesh.store.local_identity(slot, now=local.mesh.now)
    return slot, identity


def snapshot(local, message_hash):
    with local.store.connect() as db:
        wire = db.execute(
            "SELECT wire_json FROM access_mesh_messages WHERE message_hash=?", (message_hash,)
        ).fetchone()
        recipients = db.execute(
            "SELECT * FROM coordination_message_recipients WHERE message_hash=?", (message_hash,)
        ).fetchall()
    return (tuple(wire) if wire else None, [tuple(row) for row in recipients])


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
@pytest.mark.parametrize("acked", [False, True])
async def test_expiry_alert_ttl_boundary_keeps_original_wire_and_ack(mesh_pair, offset_us, acked):
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    assert await local.ensure_session_alert(slot, identity)
    message_hash = local._session_alert_hash(
        local.node_id, slot, identity["session_lifecycle"]["started_at"]
    )
    if acked:
        local.store.acknowledge(message_hash, slot.logical_agent_id, local.mesh.now.isoformat())
    original = snapshot(local, message_hash)
    identity["public_name"] = "renamed display target"
    local.mesh.now = slot.expires_at + MESSAGE_TTL + timedelta(microseconds=offset_us)
    for _ in range(2):
        assert await local.ensure_session_alert(slot, identity) is (offset_us < 0 and not acked)
        assert snapshot(local, message_hash) == original


@pytest.mark.parametrize("original_exists", [False, True])
async def test_expired_alert_is_not_recreated_after_prune_or_restart(mesh_pair, original_exists):
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    if original_exists:
        assert await local.ensure_session_alert(slot, identity)
    message_hash = local._session_alert_hash(
        local.node_id, slot, identity["session_lifecycle"]["started_at"]
    )
    local.mesh.now = slot.expires_at + MESSAGE_TTL
    local.store.prune_expired()
    restarted = AccessMeshMessaging(local.mesh, local.agent_store)
    await restarted.start()
    try:
        for _ in range(3):
            assert await restarted.ensure_session_alert(slot, identity) is False
            assert snapshot(restarted, message_hash) == (None, [])
    finally:
        await restarted.stop()


@pytest.mark.parametrize("phase", ["wire", "accept", "recipient"])
async def test_expiry_crossed_between_alert_storage_calls_is_benign(mesh_pair, monkeypatch, phase):
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    if phase != "accept":
        assert await local.ensure_session_alert(slot, identity)
    message_hash = local._session_alert_hash(
        local.node_id, slot, identity["session_lifecycle"]["started_at"]
    )
    original = snapshot(local, message_hash)
    identity["public_name"] = "renamed during retention boundary"
    local.mesh.now = slot.expires_at + MESSAGE_TTL - timedelta(microseconds=1)
    method = getattr(local.store, phase)

    def cross_boundary(*args, **kwargs):
        local.mesh.now += timedelta(microseconds=1)
        return method(*args, **kwargs)

    monkeypatch.setattr(local.store, phase, cross_boundary)
    assert await local.ensure_session_alert(slot, identity) is False
    assert snapshot(local, message_hash) == original


@pytest.mark.parametrize("code", ["message_not_found", "idempotency_conflict"])
async def test_young_alert_storage_failures_are_not_hidden(mesh_pair, monkeypatch, code):
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    assert await local.ensure_session_alert(slot, identity)

    def fail(*args):
        raise MeshMessagingError(code)

    monkeypatch.setattr(local.store, "recipient", fail)
    with pytest.raises(MeshMessagingError, match=code):
        await local.ensure_session_alert(slot, identity)


async def test_new_cycle_after_old_alert_expiry_gets_a_new_hash(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    slot, old_identity = expired_identity(local)
    assert await local.ensure_session_alert(slot, old_identity)
    old_hash = local._session_alert_hash(
        local.node_id, slot, old_identity["session_lifecycle"]["started_at"]
    )
    local.mesh.now = slot.expires_at + MESSAGE_TTL
    assert await local.ensure_session_alert(slot, old_identity) is False
    slot.started_at = local.mesh.now - timedelta(seconds=20)
    slot.expires_at = local.mesh.now - timedelta(seconds=1)
    identity = local.mesh.store.local_identity(slot, now=local.mesh.now)
    assert await local.ensure_session_alert(slot, identity)
    new_hash = local._session_alert_hash(
        local.node_id, slot, identity["session_lifecycle"]["started_at"]
    )
    assert new_hash != old_hash
    expected_creation = identity["session_lifecycle"]["hard_expires_at"]
    assert local.store.wire(new_hash)["created_at"] == expected_creation


@pytest.mark.parametrize("expiry", ["invalid", "2026-10-08T00:00:00"])
async def test_invalid_or_naive_alert_expiry_is_not_stored(mesh_pair, expiry):
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    identity["session_lifecycle"]["hard_expires_at"] = expiry
    assert await local.ensure_session_alert(slot, identity) is False
    message_hash = local._session_alert_hash(
        local.node_id, slot, identity["session_lifecycle"]["started_at"]
    )
    assert snapshot(local, message_hash) == (None, [])


def lifecycle_app(size):
    slots = [SimpleNamespace(logical_agent_id=f"agent-{i:03}") for i in range(size)]
    store = SimpleNamespace(
        trusted_issuers=frozenset(),
        defaults=lambda: None,
        local_slots=lambda after: [s for s in slots if s.logical_agent_id > after][:50],
        canonical_slot=lambda slot: slot,
        local_identity=lambda slot, now: {},
        pending_cleanup=lambda limit: [],
    )
    app = AccessMeshApplication(store, task_store=None, execution_fence=None)
    app.cleanup = AsyncMock()
    app.messages = SimpleNamespace(ensure_session_alert=AsyncMock())
    app._task = SimpleNamespace(done=lambda: False)
    return app


@pytest.mark.parametrize("failed_index", [0, 50])
async def test_failure_survives_partial_scan_and_clears_only_after_full_success(failed_index):
    app = lifecycle_app(51)

    async def fail(slot, identity):
        if slot.logical_agent_id == f"agent-{failed_index:03}":
            raise AccessMeshError("message_not_found")

    app.messages.ensure_session_alert.side_effect = fail
    await app.tick()
    await app.tick()
    assert app.last_error == "message_not_found"
    app.messages.ensure_session_alert.side_effect = None
    await app.tick()
    assert app.last_error == "message_not_found"
    assert (await app.health_components())["access_mesh"]["status"] == "degraded"
    await app.tick()
    assert app.last_error is None
    assert (await app.health_components())["access_mesh"]["status"] == "healthy"


@pytest.mark.parametrize("size", [0, 1, 50, 100])
async def test_clean_complete_scan_clears_old_error_with_exact_page_boundaries(size):
    app = lifecycle_app(size)
    app.last_error = "old_failure"
    for _ in range(size // 50):
        await app.tick()
        assert app.last_error == "old_failure"
    await app.tick()
    assert app.last_error is None


@pytest.mark.parametrize("phase", ["local_slots", "cleanup"])
async def test_outer_failure_on_last_page_cannot_be_cleared_by_retry(phase):
    app = lifecycle_app(51)
    app.last_error = "old_failure"
    await app.tick()
    original = getattr(app.store if phase == "local_slots" else app, phase)
    if phase == "local_slots":
        def fail(**kwargs):
            raise RuntimeError("scan unavailable")
        app.store.local_slots = fail
    else:
        app.cleanup = AsyncMock(side_effect=RuntimeError("cleanup unavailable"))
    with pytest.raises(RuntimeError):
        await app.tick()
    assert app.last_error == "RuntimeError"
    assert (await app.health_components())["access_mesh"]["status"] == "degraded"
    setattr(app.store if phase == "local_slots" else app, phase, original)
    # An enumeration failure retries its page; a cleanup failure has already
    # advanced the cursor and the next tick starts a fresh complete scan.
    if phase == "local_slots":
        await app.tick()
        assert app.last_error == "RuntimeError"
    await app.tick()
    assert app.last_error == "RuntimeError"
    await app.tick()
    assert app.last_error is None


async def test_repeated_cleanup_failures_do_not_starve_later_pages():
    app = lifecycle_app(101)
    healthy_cleanup = app.cleanup
    app.cleanup = AsyncMock(side_effect=RuntimeError("cleanup unavailable"))
    for expected_cursor in ["agent-049", "agent-099", ""]:
        with pytest.raises(RuntimeError):
            await app.tick()
        assert app._scan_after == expected_cursor
        assert app.last_error == "RuntimeError"
    visited = {
        call.args[0].logical_agent_id
        for call in app.messages.ensure_session_alert.await_args_list
    }
    assert len(visited) == 101
    app.cleanup = healthy_cleanup
    await app.tick()
    assert app.last_error == "RuntimeError"
    await app.tick()
    assert app.last_error == "RuntimeError"
    await app.tick()
    assert app.last_error is None


async def test_health_recovery_still_requires_worker_and_cleanup_health():
    app = lifecycle_app(0)
    app.last_error = "old_failure"
    await app.tick()
    app.store.pending_cleanup = lambda limit: [{"job_id": "pending"}]
    assert (await app.health_components())["access_mesh"]["reason"] == "cleanup_pending"
    app.store.pending_cleanup = lambda limit: []
    app._task = None
    assert (await app.health_components())["access_mesh"]["reason"] == "lifecycle_worker_stopped"

