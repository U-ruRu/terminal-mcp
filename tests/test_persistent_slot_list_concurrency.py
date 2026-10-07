import asyncio
from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator


@pytest.mark.asyncio
async def test_lifecycle_list_slots_projects_slots_with_bounded_concurrency():
    lifecycle = object.__new__(PersistentLifecycleCoordinator)
    lifecycle._available = lambda: None
    lifecycle._read_admission = lambda: None
    lifecycle.store = SimpleNamespace(
        list_slots=lambda: asyncio.sleep(
            0, result=[SimpleNamespace(logical_agent_id=f"la_{i}") for i in range(16)]
        )
    )
    active = 0
    peak = 0

    async def slot_result(logical_agent_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"slot": {"logical_agent_id": logical_agent_id}}

    lifecycle._slot_result = slot_result
    result = await lifecycle.list_slots()

    assert peak > 1
    assert peak <= 8
    assert [item["slot"]["logical_agent_id"] for item in result["slots"]] == [
        f"la_{i}" for i in range(16)
    ]


@pytest.mark.asyncio
async def test_backend_slot_list_resolves_access_with_bounded_concurrency():
    backend = object.__new__(PersistentBackend)
    slots = [{"slot": {"logical_agent_id": f"la_{i}"}} for i in range(16)]
    backend.lifecycle = SimpleNamespace(
        list_slots=lambda: asyncio.sleep(
            0, result={"ok": True, "slots": slots, "server_now": "now"}
        )
    )
    active = 0
    peak = 0

    async def access_get(logical_agent_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"public_name": logical_agent_id, "access_generation": 1, "status": "active"}

    backend._access_get = access_get
    result = await backend.slot_list()

    assert peak > 1
    assert peak <= 8
    assert [item["access"]["public_name"] for item in result["slots"]] == [
        f"la_{i}" for i in range(16)
    ]


@pytest.mark.asyncio
async def test_lifecycle_list_slots_drains_siblings_when_projection_fails():
    lifecycle = object.__new__(PersistentLifecycleCoordinator)
    lifecycle._available = lambda: None
    lifecycle._read_admission = lambda: None
    lifecycle.store = SimpleNamespace(
        list_slots=lambda: asyncio.sleep(
            0, result=[SimpleNamespace(logical_agent_id=f"la_{i}") for i in range(16)]
        )
    )
    active: set[str] = set()
    cancelled: set[str] = set()

    async def slot_result(logical_agent_id):
        active.add(logical_agent_id)
        try:
            if logical_agent_id == "la_0":
                await asyncio.sleep(0)
                raise RuntimeError("projection_failed")
            await asyncio.sleep(60)
            return {"slot": {"logical_agent_id": logical_agent_id}}
        except asyncio.CancelledError:
            cancelled.add(logical_agent_id)
            raise
        finally:
            active.discard(logical_agent_id)

    lifecycle._slot_result = slot_result

    with pytest.raises(RuntimeError, match="projection_failed"):
        await lifecycle.list_slots()

    assert active == set()
    assert cancelled


@pytest.mark.asyncio
async def test_backend_slot_list_drains_siblings_when_access_lookup_fails():
    backend = object.__new__(PersistentBackend)
    slots = [{"slot": {"logical_agent_id": f"la_{i}"}} for i in range(16)]
    backend.lifecycle = SimpleNamespace(
        list_slots=lambda: asyncio.sleep(
            0, result={"ok": True, "slots": slots, "server_now": "now"}
        )
    )
    active: set[str] = set()
    cancelled: set[str] = set()

    async def access_get(logical_agent_id):
        active.add(logical_agent_id)
        try:
            if logical_agent_id == "la_0":
                await asyncio.sleep(0)
                raise RuntimeError("access_failed")
            await asyncio.sleep(60)
            return {"public_name": logical_agent_id, "access_generation": 1, "status": "active"}
        except asyncio.CancelledError:
            cancelled.add(logical_agent_id)
            raise
        finally:
            active.discard(logical_agent_id)

    backend._access_get = access_get

    with pytest.raises(RuntimeError, match="access_failed"):
        await backend.slot_list()

    assert active == set()
    assert cancelled


@pytest.mark.asyncio
async def test_access_observe_slots_paginates_after_filtering_inactive_slots():
    backend = object.__new__(PersistentBackend)
    slots = [{"public_name": f"Agent-{index:03d}"} for index in range(70)]
    calls = []

    class Fleet:
        async def list_access_slots(self, *, limit=None, offset=0):
            calls.append((limit, offset))
            end = None if limit is None else offset + limit
            return slots[offset:end]

    backend.fleet_bridge = Fleet()
    backend.access_authority = None
    active = {"Agent-065", "Agent-067", "Agent-069"}

    async def sender_identity(public_name):
        if public_name not in active:
            return {"ok": False, "code": "session_not_found"}
        return {
            "ok": True,
            "session_state": "active",
            "session_started_at": "2026-10-07T00:00:00Z",
            "hard_expires_at": "2026-10-07T01:00:00Z",
            "remaining_seconds": 1200,
            "last_active_at": "2026-10-07T00:10:00Z",
        }

    backend.access_sender_identity = sender_identity

    first = await backend.access_observe_slots(limit=2, offset=0)
    second = await backend.access_observe_slots(limit=2, offset=1)

    assert [row["public_name"] for row in first["sessions"]] == ["Agent-065", "Agent-067"]
    assert [row["public_name"] for row in second["sessions"]] == ["Agent-067", "Agent-069"]
    assert (64, 0) in calls
    assert (64, 64) in calls
