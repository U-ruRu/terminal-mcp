"""Acceptance tests for bounded distributed number negotiation and immediate attach."""

import asyncio
from time import monotonic

import httpx
import pytest
from test_access_mesh_replication_http import Routes, actor, node

from terminal_mcp.fleet.access_mesh import AccessMeshReplication
from terminal_mcp.fleet.config import FleetConfig, FleetPeer


@pytest.mark.asyncio
async def test_issuance_broadcasts_slot_before_first_remote_attach(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        peer, peer_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issuer.replication = replication
        issued = await issuer.issue(actor())
        number = issued["access_code"]
        remote_slot = peer.store.slot("firstbyte", issued["slot_id"])
        assert remote_slot is not None, "Commit must deliver SlotIssued, not only a number claim"
        attached = await peer.attach(actor(), session_number=number)
        assert attached == {"ok": True}
        effective = peer.store.attached_slot(peer.connection_key(actor()))
        assert effective.logical_agent_id == issued["logical_agent_id"]
        assert len(peer.store.numbers.snapshot()) == 1
        assert not issuer.store.pending_outbox(peer_node_id="bacloud")


@pytest.mark.asyncio
async def test_one_fast_conflict_replaces_number_while_other_peer_hangs(tmp_path, monkeypatch):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        issuer.store.trusted_issuers = frozenset({"firstbyte", "bacloud", "slow"})
        peers = (
            FleetPeer("bacloud", "https://bacloud", "key", "token"),
            FleetPeer("slow", "https://slow", "key", "token"),
        )
        replication = AccessMeshReplication(
            issuer,
            FleetConfig("firstbyte", "key", peers, 1.0, 1.0),
        )
        attempts = []

        async def fake_request(peer, path, payload):
            assert path == "numbers/reserve"
            attempts.append((peer.instance_id, payload["number"]))
            if peer.instance_id == "slow":
                await asyncio.sleep(60)
            if payload["number"] == "0001":
                return {
                    "ok": False,
                    "code": "number_conflict",
                    "suggested_number": "0002",
                }
            return {"ok": True, "number": payload["number"]}

        monkeypatch.setattr(replication, "request", fake_request)
        start = monotonic()
        number, attempt, deadline = await replication.negotiate_number(
            preferred="0001",
            _budget_seconds=0.35,
        )
        elapsed = monotonic() - start
        assert number == "0002", attempts
        assert 0.2 <= elapsed < 0.6
        assert deadline <= start + 0.4
        assert ("bacloud", "0001") in attempts
        assert ("bacloud", "0002") in attempts
        reservations = issuer.store.numbers.reservations()
        assert {r["number"] for r in reservations} >= {"0001", "0002"}
        issuer.store.numbers.release(attempt)
        assert issuer.store.numbers.reservations() == []


@pytest.mark.asyncio
async def test_all_responding_peers_finish_without_waiting_for_deadline(tmp_path, monkeypatch):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        peers = (FleetPeer("bacloud", "https://bacloud", "key", "token"),)
        replication = AccessMeshReplication(
            issuer,
            FleetConfig("firstbyte", "key", peers, 1.0, 1.0),
        )

        async def accept(peer, path, payload):
            return {"ok": True, "number": payload["number"]}

        monkeypatch.setattr(replication, "request", accept)
        start = monotonic()
        result = await replication.negotiate_number(
            preferred="9000",
            _budget_seconds=0.8,
        )
        assert result[0] == "9000"
        assert monotonic() - start < 0.4
