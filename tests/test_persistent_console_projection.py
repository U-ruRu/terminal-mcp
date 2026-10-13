"""Service slot-enrichment projection with backend/task/audit ports doubled."""

import asyncio
from types import SimpleNamespace

import pytest

from terminal_mcp.core.service import TerminalService


@pytest.mark.asyncio
async def test_slot_enrichment_is_concurrent_and_preserves_authoritative_order():
    slot_ids = [f"logical-{index}" for index in range(16)]

    async def slot_list():
        return {
            "ok": True,
            "server_now": "2026-10-05T20:00:00Z",
            "slots": [
                {"slot": {"logical_agent_id": agent}, "work_session": None} for agent in slot_ids
            ],
        }

    active = 0
    peak = 0

    async def delayed_empty(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    service = SimpleNamespace(
        persistent=SimpleNamespace(
            slot_list=slot_list,
            lifecycle=SimpleNamespace(store=SimpleNamespace(audit_events=delayed_empty)),
        ),
        task_store=SimpleNamespace(claims_for_owner=delayed_empty),
    )
    projected = await TerminalService._persistent_console_snapshot(service, {"enabled": True})
    assert peak > 1
    assert [item["slot"]["logical_agent_id"] for item in projected["slots"]] == slot_ids
    assert all(item["claims"] == [] and item["audit"] == [] for item in projected["slots"])
