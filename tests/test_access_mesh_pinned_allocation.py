"""All configured peers must confirm new number allocation, including unknown reuse."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_access_mesh_replication_http import T0, Routes, actor, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.fleet.config import FleetPeer


@pytest.mark.asyncio
async def test_unknown_remote_pin_blocks_partition_allocation_then_releases_on_reconnect(
    tmp_path, monkeypatch
):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        requester, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        owner, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        owner.store.numbers.register(
            number="4731",
            issuer_id="bacloud",
            slot_id="expired-held-slot",
            logical_agent_id="held-agent",
            started_at=T0,
            hard_expires_at=T0 + timedelta(minutes=1),
        )
        hold = dict(
            source_node_id="bacloud",
            hold_id="sub-held",
            recipient_agent_id="held-agent",
            issuer_id="bacloud",
            slot_id="expired-held-slot",
            number="4731",
            state="held",
            revision=1,
        )
        owner.store.recipient_holds.merge([hold], authenticated_peer_id="bacloud")
        assert requester.store.numbers.winner("4731") is None
        assert requester.store.recipient_holds.snapshot() == []
        routes.offline.add("bacloud")
        with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
            await replication.negotiate_number(preferred="4731", _budget_seconds=0.15)
        assert requester.store.numbers.snapshot() == []
        assert requester.store.numbers.reservations() == []
        routes.offline.clear()
        # A responding owner rejects the pin even though its work claim expired.
        alternative, attempt, _ = await replication.negotiate_number(
            preferred="4731", _budget_seconds=0.5
        )
        assert alternative != "4731"
        requester.store.numbers.release(attempt)
        await replication.request(replication.peers[0], "numbers/release", {"attempt_id": attempt})
        # Release tombstones propagate without ever granting a new work window.
        owner.store.recipient_holds.merge(
            [{**hold, "state": "released", "revision": 2}], authenticated_peer_id="bacloud"
        )
        await replication.sync_recipient_holds(replication.peers[0])
        selected, attempt, _ = await replication.negotiate_number(
            preferred="4731", _budget_seconds=0.5
        )
        assert selected == "4731"
        requester.store.numbers.release(attempt)
        await replication.request(replication.peers[0], "numbers/release", {"attempt_id": attempt})


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_failed_new_issuance_never_changes_existing_work_session(
    tmp_path, monkeypatch, explicit
):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issuer.replication = replication
        initial = await issuer.issue(actor())
        await issuer.attach(actor(), session_number=initial["access_code"])
        before = await issuer.resolve(actor(), ManagedOperation.COMMAND_RUN)
        count = len(issuer.store.numbers.snapshot())
        original = replication.negotiate_number

        async def bounded(**kwargs):
            return await original(**kwargs, _budget_seconds=0.15)

        monkeypatch.setattr(replication, "negotiate_number", bounded)
        routes.offline.add("bacloud")
        with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
            await issuer.issue(actor(), **({"code": "7634"} if explicit else {}))
        assert len(issuer.store.numbers.snapshot()) == count
        after = await issuer.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert after["work_session_id"] == before["work_session_id"]
        assert after["hard_expires_at"] == before["hard_expires_at"]
        assert issuer.store.numbers.reservations() == []
        routes.offline.clear()
        created = await issuer.issue(actor(), **({"code": "7634"} if explicit else {}))
        assert created["ok"]
        if explicit:
            assert created["access_code"] == "7634"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"ok": True}, {"ok": True, "number": "9999"}])
async def test_ambiguous_or_mismatched_acknowledgement_cannot_prove_number_free(
    tmp_path, monkeypatch, response
):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)

        async def wrong_reply(peer, path, payload):
            return response

        monkeypatch.setattr(replication, "request", wrong_reply)
        with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
            await replication.negotiate_number(preferred="7634", _budget_seconds=0.1)
        assert issuer.store.numbers.reservations() == []
        assert issuer.store.numbers.snapshot() == []


@pytest.mark.asyncio
async def test_peer_membership_change_invalidates_the_completed_proof(tmp_path, monkeypatch):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        issuer.store.trusted_issuers = frozenset({"firstbyte", "bacloud", "other"})

        async def changed_membership(peer, path, payload):
            if path == "numbers/reserve":
                replication.config = replace(
                    replication.config,
                    peers=(
                        *replication.config.peers,
                        FleetPeer("other", "https://other", "key", "token"),
                    ),
                )
                return {"ok": True, "number": payload["number"]}
            return {"ok": True}

        monkeypatch.setattr(replication, "request", changed_membership)
        with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
            await replication.negotiate_number(preferred="7634", _budget_seconds=0.1)
        assert issuer.store.numbers.reservations() == []


@pytest.mark.asyncio
async def test_untrusted_configured_peer_cannot_be_silently_ignored(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        issuer.store.trusted_issuers = frozenset({"firstbyte"})
        with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
            await replication.negotiate_number(preferred="7634", _budget_seconds=0.1)
        assert issuer.store.numbers.reservations() == []


@pytest.mark.asyncio
async def test_local_only_exact_conflict_cleans_up_without_creating_network_tasks(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        replication.config = replace(replication.config, peers=())
        assert issuer.store.numbers.reserve(number="7634", attempt_id="other-attempt")["ok"]
        with pytest.raises(AccessMeshError, match="access_mesh_code_in_use"):
            await replication.negotiate_number(
                preferred="7634", exact=True, _budget_seconds=0.1
            )
        rows = issuer.store.numbers.reservations()
        assert len(rows) == 1 and rows[0]["attempt_id"] == "other-attempt"
