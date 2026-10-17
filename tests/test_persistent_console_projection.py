"""Service slot-enrichment projection with backend/task/audit ports doubled."""

from types import SimpleNamespace

from terminal_mcp.core.service import TerminalService


async def test_slot_projection_preserves_authoritative_order():
    from unittest.mock import AsyncMock

    slot_ids = ["logical-second", "logical-first"]
    service = SimpleNamespace(
        persistent=SimpleNamespace(
            slot_list=AsyncMock(
                return_value={
                    "ok": True,
                    "server_now": "2026-10-05T20:00:00Z",
                    "slots": [
                        {"slot": {"logical_agent_id": agent}, "work_session": None}
                        for agent in slot_ids
                    ],
                }
            ),
            lifecycle=SimpleNamespace(
                store=SimpleNamespace(audit_events=AsyncMock(return_value=[]))
            ),
        ),
        task_store=SimpleNamespace(claims_for_owner=AsyncMock(return_value=[])),
    )

    projected = await TerminalService._persistent_console_snapshot(service, {"enabled": True})

    assert [item["slot"]["logical_agent_id"] for item in projected["slots"]] == slot_ids
    assert all(item["claims"] == [] and item["audit"] == [] for item in projected["slots"])
