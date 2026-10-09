"""Acceptance: an explicit deadline extension must apply to the active Mesh cycle."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_access_mesh_replication_http import T0, Routes, actor, node

from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.core.managed_sessions import ManagedOperation


@pytest.mark.asyncio
async def test_explicit_start_deadline_extension_affects_current_cycle_on_all_nodes(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        access = replace(actor(), endpoint_role="access")
        created = await first.issue(
            access,
            code="0732",
            policy=SlotPolicy(30, 0, False),
        )
        first.clock = second.clock = lambda: T0 + timedelta(seconds=5)
        first.store.clock = second.store.clock = first.clock
        # Explicit cycle start: the next mutation extends this same cycle.
        await first.change(
            access,
            slot_id=created["slot_id"],
            kind="SessionStarted",
            expected_revision=1,
            effective_at=T0 + timedelta(seconds=5),
            deadline_at=T0 + timedelta(seconds=35),
            number_start={"active_from": T0 + timedelta(seconds=5)},
        )
        await f_rep.tick()
        await s_rep.tick()
        assert (await second.attach(actor(), session_number="0732")) == {"ok": True}
        updated = await first.change(
            access,
            slot_id=created["slot_id"],
            kind="SessionUpdated",
            expected_revision=2,
            effective_at=T0 + timedelta(seconds=5),
            deadline_at=T0 + timedelta(seconds=80),
        )
        assert updated["ok"]
        await f_rep.tick()
        await s_rep.tick()
        first.clock = second.clock = lambda: T0 + timedelta(seconds=50)
        first.store.clock = second.store.clock = first.clock

        for mesh in (first, second):
            slot = mesh.store.slot("firstbyte", created["slot_id"])
            cycle = mesh.store.merged_cycle(slot, mesh.clock())
            assert cycle["state"] in {"active", "warning"}, cycle
            assert cycle["hard_expires_at"] == mesh.store.numbers.stamp(T0 + timedelta(seconds=80))
        resolved = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert resolved["logical_agent_id"] == created["logical_agent_id"]
        assert resolved["session_lifecycle"]["hard_expires_at"] == (
            second.store.numbers.stamp(T0 + timedelta(seconds=80))
        )
