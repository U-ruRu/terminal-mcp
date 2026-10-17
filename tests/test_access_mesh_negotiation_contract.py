"""Acceptance tests for bounded distributed number negotiation and immediate attach."""

import asyncio

import httpx
import pytest
from test_access_mesh_replication_http import Routes, node


@pytest.mark.asyncio
async def test_confirmed_reservation_retains_requested_number(tmp_path, monkeypatch):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)

        async def accept(peer, path, payload):
            return {"ok": True, "number": payload["number"]}

        monkeypatch.setattr(replication, "request", accept)
        result = await replication.negotiate_number(preferred="9000")
        assert result[0] == "9000"


@pytest.mark.asyncio
async def test_late_conflict_reply_records_number_incident(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        reply = asyncio.get_running_loop().create_future()
        reply.set_result({"ok": False, "code": "number_conflict", "suggested_number": "0098"})
        replication._record_late_reservation_reply(
            reply, number="0097", attempt_id="nr_refused", peer_id="bacloud"
        )
        (incident,) = issuer.store.numbers.incidents()
        assert (incident["number"], incident["slot_id"], incident["collided_with"]) == (
            "0097",
            "nr_refused",
            "late_reservation:bacloud:0098",
        )
