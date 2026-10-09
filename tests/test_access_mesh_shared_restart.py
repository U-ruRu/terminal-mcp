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
