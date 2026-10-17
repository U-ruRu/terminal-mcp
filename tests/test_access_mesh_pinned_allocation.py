"""All configured peers must confirm new number allocation, including unknown reuse."""

from dataclasses import replace

import httpx
import pytest
from test_access_mesh_replication_http import Routes, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.fleet.config import FleetPeer


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
