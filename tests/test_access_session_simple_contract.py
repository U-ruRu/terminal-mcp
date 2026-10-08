"""Access issuer session window behavior is independent of JSON-RPC ids."""

from datetime import timedelta

import pytest
from test_access_mesh_native_lifecycle import T0, actor
from test_access_mesh_native_lifecycle import fixture as _native_fixture

from terminal_mcp.core.access_mesh_grants import SlotPolicy

fixture = _native_fixture


@pytest.mark.asyncio
async def test_early_end_and_start_during_cooldown_preserves_original_deadline(fixture):
    f = fixture
    f.app.defaults = SlotPolicy(20, 7, True)
    caller = actor("access")
    original = await f.app.issuer_session(caller, action="start", mode="legacy")
    assert original["ok"] and len(original["access_code"]) == 4
    slot = f.store.issuer_bound_slot(f.app.connection_key(caller))
    assert slot is not None
    initial_deadline = slot.anchor + timedelta(seconds=20)

    duplicate = await f.app.issuer_session(caller, action="start", mode="legacy")
    assert duplicate == {
        "ok": False, "code": "session_already_started",
        "error": "session_already_started",
    }
    assert f.store.issuer_bound_slot(f.app.connection_key(caller)).slot_id == slot.slot_id

    f.clock[0] = T0 + timedelta(seconds=2)
    ended = await f.app.issuer_session(caller, action="end")
    assert ended["ok"]
    f.clock[0] = T0 + timedelta(seconds=4)
    resumed = await f.app.issuer_session(caller, action="start", mode="legacy")
    assert resumed["ok"]
    current = f.store.issuer_bound_slot(f.app.connection_key(caller))
    assert current.slot_id == slot.slot_id
    assert current.deadline_at == initial_deadline
    f.clock[0] = initial_deadline + timedelta(seconds=1)
    expired = await f.app.issuer_session(caller, action="start", mode="legacy")
    assert expired["code"] == "session_expired" and expired["return_to_chat"]


@pytest.mark.asyncio
async def test_after_cooldown_start_opens_full_window_without_second_slot(fixture):
    f = fixture
    f.app.defaults = SlotPolicy(20, 5, True)
    caller = actor("access")
    original = await f.app.issuer_session(caller, action="start", mode="legacy")
    slot = f.store.issuer_bound_slot(f.app.connection_key(caller))
    assert original["ok"]
    f.clock[0] = T0 + timedelta(seconds=1)
    assert (await f.app.issuer_session(caller, action="end"))["ok"]
    f.clock[0] = T0 + timedelta(seconds=7)
    started = await f.app.issuer_session(caller, action="start", mode="legacy")
    assert started["ok"]
    same_slot = f.store.issuer_bound_slot(f.app.connection_key(caller))
    assert same_slot.slot_id == slot.slot_id
    assert same_slot.deadline_at == f.clock[0] + timedelta(seconds=20)
