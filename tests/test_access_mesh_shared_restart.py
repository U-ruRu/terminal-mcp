"""End and manual restart of merged session must converge on every peer."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_access_mesh_replication_http import T0, Routes, actor, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError, SlotPolicy
from terminal_mcp.core.managed_sessions import ManagedOperation


@pytest.mark.asyncio
async def test_cross_server_end_and_manual_start_reuses_original_shared_deadline(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issuer = replace(actor(), endpoint_role="access")
        # Two partition-issued slots with the same number, bound to different
        # Access issuers; catch up must elect a shared LogicalAgent.
        spec_a = first._receipt_spec(issuer, {"action": "start", "mode": None, "code": None})
        spec_b = second._receipt_spec(issuer, {"action": "start", "mode": None, "code": None})
        a = await first.issue(
            issuer,
            code="0307",
            receipt_spec=spec_a,
            policy=SlotPolicy(240, 20, True),
        )
        b = await second.issue(
            issuer,
            code="0307",
            receipt_spec=spec_b,
            policy=SlotPolicy(600, 20, True),
        )
        assert a["slot_id"] != b["slot_id"]
        first.replication = f_rep
        second.replication = s_rep
        await f_rep.tick()
        await s_rep.tick()
        assert first.store.numbers.winner("0307") == second.store.numbers.winner("0307")
        await second.attach(actor(), session_number="0307")
        original = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        shared_deadline = original["session_lifecycle"]["hard_expires_at"]
        assert shared_deadline == first.store.numbers.stamp(T0 + timedelta(minutes=10))

        clock = [T0 + timedelta(seconds=45)]
        first.clock = second.clock = lambda: clock[0]
        first.store.clock = second.store.clock = first.clock
        assert await first.issuer_session(issuer, action="end") == {"ok": True}
        with pytest.raises(AccessMeshError):
            await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        clock[0] += timedelta(milliseconds=1)
        resumed = await first.issuer_session(issuer, action="start")
        assert resumed == {"ok": True, "session_number": "0307"}
        restored = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert restored["logical_agent_id"] == original["logical_agent_id"]
        assert restored["session_lifecycle"]["hard_expires_at"] == shared_deadline
        # A second manual end of the same original cycle must propagate as
        # another distinct event; first ACK must not mask this later fence.
        clock[0] = T0 + timedelta(seconds=65)
        assert await first.issuer_session(issuer, action="end") == {"ok": True}
        assert len(first.store.numbers.end_snapshot()) == 2
        assert len(second.store.numbers.end_snapshot()) == 2
        with pytest.raises(AccessMeshError):
            await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        clock[0] = T0 + timedelta(seconds=70)
        repeated_resume = await first.issuer_session(issuer, action="start")
        assert repeated_resume == {"ok": True, "session_number": "0307"}
        assert (await second.resolve(actor(), ManagedOperation.COMMAND_RUN))[
            "logical_agent_id"
        ] == original["logical_agent_id"]

        # Even after the initial deadline, automatic rearm is forbidden.
        clock[0] = T0 + timedelta(minutes=10, seconds=21)
        with pytest.raises(AccessMeshError):
            await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        fresh = await first.issuer_session(issuer, action="start")
        assert fresh == {"ok": True, "session_number": "0307"}
        changed = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert changed["session_lifecycle"]["hard_expires_at"] == first.store.numbers.stamp(
            clock[0] + timedelta(seconds=240)
        )


@pytest.mark.asyncio
async def test_partition_recovery_replays_every_separate_end_of_original_cycle(tmp_path):
    """Two ends separated by a restart survive outage and late catch-up."""
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issuer = replace(actor(), endpoint_role="access")
        spec = first._receipt_spec(
            issuer,
            {
                "action": "start",
                "mode": None,
                "code": None,
            },
        )
        granted = await first.issue(
            issuer,
            code="0321",
            receipt_spec=spec,
            policy=SlotPolicy(60, 0, True),
        )
        first.replication = f_rep
        second.replication = s_rep
        await f_rep.tick()
        await s_rep.tick()
        await second.attach(actor(), session_number="0321")
        now = [T0 + timedelta(seconds=5)]
        first.clock = second.clock = lambda: now[0]
        first.store.clock = second.store.clock = first.clock
        routes.offline.add("bacloud")
        assert await first.issuer_session(issuer, action="end") == {"ok": True}
        now[0] += timedelta(seconds=5)
        restarted = await first.issuer_session(issuer, action="start")
        assert restarted["session_number"] == "0321"
        now[0] += timedelta(seconds=5)
        assert await first.issuer_session(issuer, action="end") == {"ok": True}
        assert len(first.store.numbers.end_snapshot()) == 2
        assert second.store.numbers.end_snapshot() == []

        routes.offline.remove("bacloud")
        # Force the next bounded anti-entropy pass. Production performs
        # it on a 30-second schedule after a disconnected peer recovers.
        s_rep._last_snapshot_pass["firstbyte"] = 0
        await s_rep.tick()
        await f_rep.tick()
        await s_rep.tick()
        assert sorted(e["ended_at"] for e in first.store.numbers.end_snapshot()) == sorted(
            e["ended_at"] for e in second.store.numbers.end_snapshot()
        )
        assert len(second.store.numbers.end_snapshot()) == 2
        with pytest.raises(AccessMeshError, match="session_expired"):
            await second.resolve(actor(), ManagedOperation.COMMAND_RUN)

        now[0] += timedelta(seconds=5)
        assert (await first.issuer_session(issuer, action="start"))["session_number"] == "0321"
        await f_rep.tick()
        await s_rep.tick()
        current = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert current["logical_agent_id"] == granted["logical_agent_id"]
