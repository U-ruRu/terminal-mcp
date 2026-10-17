"""Atomic upgrade unions, stale-scope rejection and application-history retention."""

import hashlib
import json
from dataclasses import asdict, replace

import httpx
import pytest
from test_access_mesh_registry_storage import registry, seed_slot
from test_access_mesh_replication_http import Routes, actor, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy
from terminal_mcp.core.access_mesh_scope import REGISTRY_NAMESPACE
from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore
from terminal_mcp.storage.access_mesh_registry_upgrade import (
    complete_upgrade,
    migrate_receipt_kinds,
)


def pending(store):
    with store._connect() as db:
        db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=1")


@pytest.mark.asyncio
async def test_pending_union_preserves_slot_revisions_until_policy_completion(tmp_path):
    from terminal_mcp.storage.sqlite import SqliteRepository

    await SqliteRepository(tmp_path / "registry.db", tmp_path / "output.db").initialize()
    store = registry(tmp_path / "registry.db")
    store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    seed_slot(store)
    with store._connect() as db:
        db.execute("UPDATE access_mesh_slot_replicas SET inherit_policy=1")
    before = store.registry_slot("slot-a")
    pending(store)
    policy = SlotPolicy(20, 10)
    store.sync_parent_defaults(
        {
            "source": "mesh",
            "revision": 2,
            "policy": asdict(policy),
            "mesh_id": "mesh-x",
            "control_node_id": "a",
        }
    )
    assert store.registry_slot("slot-a") == before
    complete_upgrade(store)
    after = store.registry_slot("slot-a")
    assert after.revision == before.revision + 1 and after.policy == policy
    assert not store.registry_upgrade_pending
    complete_upgrade(store)
    assert store.registry_slot("slot-a") == after


@pytest.mark.asyncio
async def test_member_runtime_import_is_once_and_does_not_enable_local_issuance(tmp_path):
    from terminal_mcp.storage.sqlite import SqliteRepository

    path = tmp_path / "registry.db"
    await SqliteRepository(path, tmp_path / "output.db").initialize()
    store = registry(path, "b")
    store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    agent_id = "preserved-local-agent"
    digest = hashlib.sha256(agent_id.encode()).hexdigest()[:32]
    slot_id = "upgrade-" + digest
    event = AccessSlotEvent(
        issuer_id="b",
        slot_id=slot_id,
        logical_agent_id=agent_id,
        revision=1,
        event_id="runtime-upgrade-" + slot_id,
        kind="SlotIssued",
        policy=SlotPolicy(10, 5),
        code_tag=store.code_tag("b", "1234"),
    )
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO logical_agents(logical_agent_id,display_name,authority_node_id,"
            "authority_epoch,created_at,updated_at) VALUES(?,'retained','b',1,'old','old')",
            (agent_id,),
        )
        db.execute("INSERT INTO access_mesh_native_owners VALUES(?,'b',?)", (agent_id, slot_id))
        pending_number = {
            "number": "1234",
            "attempt_id": "runtime-upgrade-" + digest,
            "started_at": store.clock(),
        }
        db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=0")
        with pytest.raises(AccessMeshError, match="mesh_runtime_import_invalid"):
            store.import_runtime_event(
                event, _connection=db, issued_kind="persistent", mutation_number=pending_number
            )
        db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=1")
        store.import_runtime_event(
            event, _connection=db, issued_kind="persistent", mutation_number=pending_number
        )
        with pytest.raises(AccessMeshError, match="access_mesh_untrusted_issuer"):
            store.apply_event(
                event, authenticated_peer_id="b", issued_kind="persistent", _connection=db
            )
        db.execute("CREATE TABLE unified_runtime_migration(singleton INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO unified_runtime_migration VALUES(1)")
        with pytest.raises(AccessMeshError, match="mesh_runtime_import_invalid"):
            store.import_runtime_event(
                event, _connection=db, issued_kind="persistent", mutation_number=pending_number
            )
    assert store.registry_slot(slot_id).logical_agent_id == agent_id
    assert store.registry_authority_id == "a"


@pytest.mark.asyncio
async def test_upgrade_preserves_a_slot_missing_from_the_former_primary(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, transport = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, _ = await node(
            tmp_path,
            "bacloud",
            "firstbyte",
            routes,
            client,
            proof_key=b"member-old-proof-retained-only-in-this-Mesh",
        )
        # Reconstruct the previous independent issuer before unified startup.
        member.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="bacloud")
        issued = await member.issue(actor(), code="1234", kind="persistent")
        member.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
        before = member.store.export_registry()["tables"]
        for store in (primary.store, member.store):
            pending(store)
        with pytest.raises(AccessMeshError, match="mesh_registry_migrating"):
            await primary.issuer_session(replace(actor(), endpoint_role="access"), action="start")
        await transport.consolidate_registry()
        await primary.attach(actor(), session_number="1234")
        assert (
            primary.store.attached_slot(primary.connection_key(actor())).slot_id
            == issued["slot_id"]
        )
        assert member.store.export_registry()["tables"] == before
        assert not primary.store.registry_upgrade_pending
        with primary.store._connect() as db:
            receipt = db.execute(
                "SELECT details_json FROM access_mesh_migration_receipts "
                "WHERE name='authoritative_registry_v1:fixture-mesh'"
            ).fetchone()
        assert json.loads(receipt[0])["registry_rows"]["access_mesh_slot_replicas"] == 1


@pytest.mark.asyncio
async def test_upgrade_number_collision_is_closed_and_preserves_both_origins(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, transport = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        await primary.issue(actor(), code="1234", kind="persistent")
        member.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="bacloud")
        await member.issue(actor(), code="1234", kind="persistent")
        member.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
        before = member.store.export_registry()["tables"]
        pending(primary.store)
        pending(member.store)
        for _ in range(2):
            with pytest.raises(AccessMeshError, match="mesh_registry_number_conflict"):
                await transport.consolidate_registry()
        assert primary.store.registry_upgrade_pending
        assert len(primary.store.issuer_slots()) == 2
        assert member.store.export_registry()["tables"] == before
        with pytest.raises(AccessMeshError, match="mesh_registry_migrating"):
            await primary.attach(actor(), session_number="1234")


@pytest.mark.asyncio
async def test_upgrade_waits_for_every_member_without_enabling_local_admission(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, transport = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        pending(primary.store)
        routes.offline.add("bacloud")
        await transport.tick()
        assert primary.store.registry_upgrade_pending
        with pytest.raises(AccessMeshError, match="mesh_registry_migrating"):
            await primary.issuer_session(replace(actor(), endpoint_role="access"), action="start")
        assert primary.store.issuer_slots() == []


def test_old_scope_cannot_commit_after_a_move_has_finished(tmp_path):
    store = registry(tmp_path / "state.db")
    store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    token = REGISTRY_NAMESPACE.set("mesh-x")
    try:
        store.begin_topology_transition("move")
        store.finish_topology_transition(
            "move", namespace_id="mesh-y", mesh_id="mesh-y", authority_id="a"
        )
        event = AccessSlotEvent(
            issuer_id="a",
            slot_id="old",
            logical_agent_id="old-agent",
            revision=1,
            event_id="old-event",
            kind="SlotIssued",
            policy=SlotPolicy(10, 5),
            code_tag=store.code_tag("a", "1234"),
        )
        with pytest.raises(AccessMeshError, match="mesh_namespace_mismatch"):
            store.apply_event(event, authenticated_peer_id="a", issued_kind="persistent")
        with pytest.raises(AccessMeshError, match="mesh_namespace_mismatch"):
            store.numbers.reserve(number="1234", attempt_id="old-request")
        assert store.registry_slot("old") is None
    finally:
        REGISTRY_NAMESPACE.reset(token)


def test_move_archives_content_and_removes_current_message_authority(tmp_path):
    path = tmp_path / "state.db"
    store = registry(path)
    store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    messages = AccessMeshMessageStore(path, "a")
    wire = {
        "text": "retained work result",
        "sender_name": "Agent 1234",
        "target": "Agent 5678",
        "created_at": "2026-10-17T12:00:00+00:00",
        "sender_id": "private-binding",
    }
    with store._connect() as db:
        db.execute(
            "INSERT INTO access_mesh_messages(message_hash,wire_json,origin_node_id,receipt_json) "
            "VALUES('message',?,'a',?)",
            (json.dumps(wire), '{"authority":"old"}'),
        )
    store.begin_topology_transition("detach")
    for _ in range(2):
        store.finish_topology_transition("detach", namespace_id="standalone-a", authority_id="a")
    assert messages.wire("message") is None
    with store._connect() as db:
        rows = db.execute("SELECT namespace_id,content_json FROM mesh_message_history").fetchall()
    assert len(rows) == 1 and rows[0][0] == "mesh-x"
    archived = json.loads(rows[0][1])
    assert archived["text"] == wire["text"] and "sender_id" not in archived


def test_receipt_kind_conversion_is_once_and_preserves_replay_identity(tmp_path):
    store = registry(tmp_path / "state.db")
    receipt = store.prepare_receipt(
        event_id="event",
        request_key="request",
        fingerprint="fingerprint",
        connection_key=None,
        result={"issuer_node_id": "a", "slot_id": "slot", "mode": "legacy"},
    )
    store.save_receipt(receipt)
    with store._connect() as db:
        db.execute("UPDATE access_mesh_registry_scope SET receipts_migrated=0")
    migrate_receipt_kinds(store)
    with store._connect() as db:
        first = tuple(db.execute("SELECT * FROM access_mesh_issuer_receipts").fetchone())
    migrate_receipt_kinds(store)
    with store._connect() as db:
        second = tuple(db.execute("SELECT * FROM access_mesh_issuer_receipts").fetchone())
    assert first == second
    assert store.receipt("request", "fingerprint")["mode"] == "temporary"


def test_membership_removal_does_not_release_an_archived_issuers_number(tmp_path):
    store = registry(tmp_path / "state.db", "b")
    store.install_registry_scope(mesh_id="mesh-x", authority_id="b")
    seed_slot(store)  # Historic issuer a has left; its suspended slot remains.
    result = store.numbers.reserve(number="1234", attempt_id="new")
    assert result["ok"] is False and result["suggested_number"] != "1234"


@pytest.mark.asyncio
async def test_handoff_materializes_unseen_native_identity_and_keeps_the_fence(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issued = await primary.issue(actor(), code="1234", kind="persistent")
        member.store.begin_topology_transition("handoff")
        member.store.import_registry(primary.store.export_registry())
        with pytest.raises(AccessMeshError, match="mesh_transition_pending"):
            member.store.assert_registry_ready()
        with member.store._connect() as db:
            native = db.execute(
                "SELECT logical_agent_id FROM access_mesh_native_owners WHERE slot_id=?",
                (issued["slot_id"],),
            ).fetchone()
        assert native[0] == issued["logical_agent_id"]


@pytest.mark.asyncio
async def test_fresh_member_attach_resolves_a_rotated_code_at_the_primary(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, replication = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        member.replication = replication
        issued = await primary.issue(actor(), code="1234", kind="persistent")
        rotated = await primary.change(
            actor(), slot_id=issued["slot_id"], kind="AccessCodeRotated", expected_revision=1
        )
        await member.attach(actor(), session_number=rotated["access_code"])
        assert (
            member.store.attached_slot(member.connection_key(actor())).slot_id == issued["slot_id"]
        )


@pytest.mark.asyncio
async def test_current_member_cannot_create_an_independent_mesh_grant(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        member, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        event = AccessSlotEvent(
            issuer_id="bacloud",
            slot_id="independent",
            logical_agent_id="intruder",
            revision=1,
            event_id="independent",
            kind="SlotIssued",
            policy=SlotPolicy(10, 5),
            code_tag=member.store.code_tag("bacloud", "1234"),
        )
        with pytest.raises(AccessMeshError, match="access_mesh_untrusted_issuer"):
            member.store.apply_event(
                event, authenticated_peer_id="bacloud", issued_kind="persistent"
            )
        assert member.store.registry_slot("independent") is None


@pytest.mark.asyncio
async def test_move_retires_native_authority_and_can_return_to_its_own_mesh(tmp_path):
    from datetime import timedelta

    from test_access_mesh_replication_http import T0

    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        mesh, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        issued = await mesh.issue(actor(), code="1234", kind="persistent", policy=SlotPolicy(10, 5))
        from terminal_mcp.core.persistent_agents import ClaimOwner

        await mesh.task_store.create_task("work", "task", "Retained task", state="in_progress")
        await mesh.task_store.claim_owner(
            "work", "task", ClaimOwner.logical_agent(issued["logical_agent_id"])
        )
        snapshot = mesh.store.export_registry()
        mesh.store.clock = lambda: T0 + timedelta(seconds=20)
        mesh.store.begin_topology_transition("leave")
        mesh.store.finish_topology_transition(
            "leave", namespace_id="standalone-new", authority_id="firstbyte"
        )
        with mesh.store._connect() as db:
            state = db.execute(
                "SELECT state FROM logical_agents WHERE logical_agent_id=?",
                (issued["logical_agent_id"],),
            ).fetchone()[0]
        assert state == "deleted" and mesh.store.registry_slot(issued["slot_id"]) is None
        assert await mesh.task_store.active_claims("work", "task") == []
        assert (await mesh.task_store.get_task("work", "task"))["state"] == "in_progress"
        mesh.store.begin_topology_transition("return")
        mesh.store.finish_topology_transition(
            "return", namespace_id="fixture-mesh", mesh_id="fixture-mesh", authority_id="firstbyte"
        )
        mesh.store.import_registry(snapshot)
        assert (
            mesh.store.registry_slot(issued["slot_id"]).logical_agent_id
            == issued["logical_agent_id"]
        )
