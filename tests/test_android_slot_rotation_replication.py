"""Real internal HTTP replication of operator rotation without a new number identity."""

from dataclasses import replace

import httpx
import pytest
from test_access_mesh_replication_http import T0, Routes, actor, node

from terminal_mcp.application.access_mesh_operator import AccessMeshOperator
from terminal_mcp.core.access_mesh_grants import AccessMeshError


@pytest.mark.asyncio
async def test_rotated_code_reaches_peer_without_changing_claim_or_live_binding(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, delivery = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        peer, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issuer.replication = delivery
        issuer.store.clock = peer.store.clock = lambda: T0
        operator = AccessMeshOperator(issuer)
        operator_actor = replace(actor(), endpoint_role="operator")
        issued = await operator.mutate(
            operator_actor,
            action="create",
            mode="persistent",
            idempotency_key="peer-rotation-create",
        )
        assert (await peer.attach(actor(), session_number=issued["access_code"]))["ok"] is True
        slot = peer.store.slot("firstbyte", issued["slot_id"])
        before = peer.store.observed_identity(slot, now=T0)
        claims = peer.store.numbers.snapshot()
        rotated = await operator.mutate(
            operator_actor,
            action="rotate",
            slot_id=issued["slot_id"],
            expected_revision=1,
            idempotency_key="peer-rotation-key",
        )
        assert bool(claims == peer.store.numbers.snapshot())
        with peer.store._connect() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM access_mesh_number_reservations").fetchone()[0]
                == 0
            )
        assert not issuer.store.pending_outbox(peer_node_id="bacloud")
        fresh = replace(
            actor(), provider_metadata={"openai/subject": "s", "openai/session": "fresh-rotation"}
        )
        assert (await peer.attach(fresh, session_number=rotated["access_code"]))["ok"] is True
        after = peer.store.observed_identity(peer.store.slot("firstbyte", slot.slot_id), now=T0)
        assert after["work_session_id"] == before["work_session_id"]
        assert after["hard_expires_at"] == before["hard_expires_at"]
        rejected = replace(
            actor(), provider_metadata={"openai/subject": "s", "openai/session": "retired-rotation"}
        )
        with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
            await peer.attach(rejected, session_number=issued["access_code"])
        replay = await operator.mutate(
            operator_actor,
            action="rotate",
            slot_id=issued["slot_id"],
            expected_revision=1,
            idempotency_key="peer-rotation-key",
        )
        assert bool(replay == rotated)
        assert peer.store.slot("firstbyte", slot.slot_id).revision == 2


@pytest.mark.asyncio
async def test_offline_peer_does_not_allow_unconfirmed_rotation(tmp_path, monkeypatch):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, delivery = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        peer, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issuer.replication = delivery
        issuer.store.clock = peer.store.clock = lambda: T0
        operator = AccessMeshOperator(issuer)
        operator_actor = replace(actor(), endpoint_role="operator")
        issued = await operator.mutate(
            operator_actor,
            action="create",
            mode="persistent",
            idempotency_key="offline-rotation-create",
        )
        reserve = delivery.negotiate_number

        async def bounded():
            return await reserve(_budget_seconds=0.05)

        monkeypatch.setattr(delivery, "negotiate_number", bounded)
        routes.offline.add("bacloud")
        with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
            await operator.mutate(
                operator_actor,
                action="rotate",
                slot_id=issued["slot_id"],
                expected_revision=1,
                idempotency_key="offline-rotation-key",
            )
        assert issuer.store.slot("firstbyte", issued["slot_id"]).revision == 1
        assert peer.store.slot("firstbyte", issued["slot_id"]).revision == 1
        assert bool(
            issuer.store.code_slot("firstbyte", issued["access_code"]).slot_id == issued["slot_id"]
        )
