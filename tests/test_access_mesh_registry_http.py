"""The minimal public ACS forwarding boundary; unrelated transports are doubled."""

from dataclasses import replace

import httpx
import pytest
from test_access_mesh_replication_http import Routes, actor, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError


@pytest.mark.asyncio
async def test_member_acs_issues_only_in_the_primary_registry(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, replica = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        for mesh in (primary, member):
            mesh.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
        member.replication = replica
        result = await member.issuer_session(
            replace(actor(), endpoint_role="access"), action="start"
        )
        assert len(result["session_number"]) == 4
        assert primary.store.numbers.winner(result["session_number"])["issuer_id"] == "firstbyte"
        assert member.store.issuer_slots() == []


@pytest.mark.asyncio
async def test_member_attach_fetches_a_primary_slot_before_replication(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, replica = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        for mesh in (primary, member):
            mesh.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
        member.replication = replica
        issued = await primary.issue(actor(), code="1234", kind="persistent")
        await member.attach(actor(), session_number="1234")
        assert (
            member.store.attached_slot(member.connection_key(actor())).slot_id == issued["slot_id"]
        )


@pytest.mark.asyncio
async def test_primary_rejects_registration_for_another_namespace(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, replica = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        primary.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
        # A trusted transport credential alone cannot cross the Mesh boundary.
        with member.store._connect() as db:
            db.execute(
                "UPDATE access_mesh_registry_scope SET namespace_id='mesh-y',mesh_id='mesh-y'"
            )
        member.store._load_registry_scope()
        member.replication = replica
        with pytest.raises(AccessMeshError, match="mesh_namespace_mismatch"):
            await member.issuer_session(replace(actor(), endpoint_role="access"), action="start")
        assert primary.store.issuer_slots() == []


@pytest.mark.asyncio
async def test_member_cannot_fall_back_to_local_issuance_when_primary_is_down(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        member, replica = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        member.store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
        member.replication = replica
        routes.offline.add("firstbyte")
        with pytest.raises(AccessMeshError, match="mesh_primary_unavailable"):
            await member.issuer_session(replace(actor(), endpoint_role="access"), action="start")
        assert member.store.issuer_slots() == []


@pytest.mark.asyncio
async def test_member_acs_end_closes_the_shared_cycle(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        primary, primary_replica = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        member, member_replica = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        primary.replication = primary_replica
        member.replication = member_replica
        access = replace(actor(), endpoint_role="access")
        issued = await member.issuer_session(access, action="start")
        await member.attach(actor(), session_number=issued["session_number"])
        assert await member.issuer_session(access, action="end") == {"ok": True}
        slot = member.store.attached_slot(member.connection_key(actor()))
        assert member.store.merged_cycle(slot, member.clock())["state"] == "expired"


def test_previous_primary_cannot_commit_cycle_after_handoff(tmp_path):
    from test_access_mesh_registry_storage import registry
    from test_access_mesh_replication_http import T0

    store = registry(tmp_path / "registry.db", "b")
    store.trusted_issuers = frozenset({"a", "b"})
    store.install_registry_scope(mesh_id="mesh-x", authority_id="a")
    store.begin_topology_transition("handoff")
    store.finish_topology_transition(
        "handoff", namespace_id="mesh-x", mesh_id="mesh-x", authority_id="b", clear_local=False
    )
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_issuer"):
        store.numbers.record_end(
            number="1234",
            cycle_key="cycle",
            issuer_id="a",
            slot_id="old",
            event_id="late-end",
            ended_at=T0,
            source_peer_id="a",
        )
