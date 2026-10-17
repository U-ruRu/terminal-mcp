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
