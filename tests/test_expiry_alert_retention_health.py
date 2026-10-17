"""Regression coverage for expiry-alert retention and full-sweep health recovery."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair_fixture

from terminal_mcp.application.access_mesh import AccessMeshApplication
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


@pytest.mark.parametrize("offset_us,expected", [(-1, True), (0, False)])
async def test_expiry_alert_obeys_retention_boundary(mesh_pair, offset_us, expected):
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    await local.ensure_session_alert(slot, identity)
    message_hash = local._session_alert_hash(
        local.node_id, slot, identity["session_lifecycle"]["started_at"]
    )
    original = snapshot(local, message_hash)
    local.mesh.now = slot.expires_at + MESSAGE_TTL + timedelta(microseconds=offset_us)

    assert await local.ensure_session_alert(slot, identity) is expected
    assert snapshot(local, message_hash) == original


async def test_young_alert_storage_failure_is_reported(mesh_pair, monkeypatch):
    code = "idempotency_conflict"
    local = mesh_pair.nodes["firstbyte"]
    slot, identity = expired_identity(local)
    assert await local.ensure_session_alert(slot, identity)

    def fail(*args):
        raise MeshMessagingError(code)

    monkeypatch.setattr(local.store, "recipient", fail)
    with pytest.raises(MeshMessagingError, match=code):
        await local.ensure_session_alert(slot, identity)


def lifecycle_app(size):
    slots = [SimpleNamespace(logical_agent_id=f"agent-{i:03}") for i in range(size)]
    store = SimpleNamespace(
        namespace_id="health-fixture",
        trusted_issuers=frozenset(),
        defaults=lambda: None,
        local_slots=lambda after: [s for s in slots if s.logical_agent_id > after][:50],
        canonical_slot=lambda slot: slot,
        local_identity=lambda slot, now: {},
        pending_cleanup=lambda limit: [],
        registry_readiness=lambda: {"ready": True, "reason": None},
    )
    app = AccessMeshApplication(store, task_store=None, execution_fence=None)
    app.cleanup = AsyncMock()
    app.messages = SimpleNamespace(ensure_session_alert=AsyncMock())
    app._task = SimpleNamespace(done=lambda: False)
    return app


async def test_completed_lifecycle_scan_clears_previous_error():
    app = lifecycle_app(0)
    app.last_error = "old_failure"

    await app.tick()

    assert app.last_error is None


async def test_stopped_lifecycle_worker_reports_unhealthy():
    app = lifecycle_app(0)
    app._task = None

    assert (await app.health_components())["access_mesh"]["reason"] == "lifecycle_worker_stopped"
