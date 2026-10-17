"""Intent declarations and grace belong to one current local work session."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import pytest_asyncio
from test_access_mesh_native_lifecycle import T0, actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.core.access_mesh_grants import AccessMeshError, SlotPolicy
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.storage.access_mesh import AccessMeshStore


@pytest_asyncio.fixture
async def intent_env(fixture):
    f = fixture
    issued = await f.app.issue(
        actor("access"), kind="persistent", code="1234", policy=SlotPolicy(1200)
    )
    await f.app.attach(actor(), session_number="1234")
    f.slot = f.store.slot("firstbyte", issued["slot_id"])
    f.session_id = f.store.observed_identity(f.slot, now=f.clock[0])["work_session_id"]
    return f


def declare(f, at=T0, text="Verify the current operation"):
    return f.store.update_intent(f.slot, f.session_id, text, now=at)


def test_missing_intent_blocks_a_write(intent_env):
    f = intent_env
    assert f.store.intent_status(f.slot, now=T0, require_intent=True) == {
        "state": "missing",
        "blocked": True,
        "warn": False,
    }


def test_first_observation_at_refresh_deadline_grants_one_warning(intent_env):
    f = intent_env
    declare(f)
    status = f.store.intent_status(
        f.slot, now=T0 + timedelta(seconds=180), observe=True, require_intent=True
    )
    assert (status["state"], status["warn"], status["blocked"]) == ("stale", True, False)


def test_concurrent_observers_cannot_share_grace(intent_env):
    f = intent_env
    declare(f)

    def observe():
        return f.store.intent_status(
            f.slot, now=T0 + timedelta(seconds=180), observe=True, require_intent=True
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(observe)
        second = pool.submit(observe)
        statuses = [first.result(), second.result()]
    assert sorted((status["warn"], status["blocked"]) for status in statuses) == [
        (False, True),
        (True, False),
    ]


def test_consumed_grace_remains_blocked_after_store_reopen(intent_env):
    f = intent_env
    declare(f)
    now = T0 + timedelta(seconds=180)
    f.store.intent_status(f.slot, now=now, observe=True)
    reopened = AccessMeshStore(
        f.store.path,
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=__import__("test_access_mesh_native_lifecycle").KEY,
    )
    assert reopened.intent_status(f.slot, now=now, require_intent=True)["blocked"]


def test_repeated_initial_declaration_preserves_original_refresh_deadline(intent_env):
    f = intent_env
    original = declare(f)
    repeated = f.store.update_intent(
        f.slot,
        f.session_id,
        "Another attach",
        now=T0 + timedelta(seconds=180),
        initial_only=True,
    )
    assert repeated == original


def test_explicit_intent_refresh_clears_consumed_warning(intent_env):
    f = intent_env
    declare(f)
    now = T0 + timedelta(seconds=180)
    f.store.intent_status(f.slot, now=now, observe=True)
    f.store.update_intent(f.slot, f.session_id, "Continue current work", now=now)
    status = f.store.intent_status(f.slot, now=now, require_intent=True)
    assert (status["state"], status["warn"], status["blocked"]) == ("fresh", False, False)


@pytest.mark.asyncio
async def test_grace_cannot_cross_a_replaced_declaration(intent_env):
    f = intent_env
    declare(f)
    f.clock[0] = T0 + timedelta(seconds=180)
    old_grace = await f.app.observe_intent_call(actor(), require_intent=True)
    declare(f, at=f.clock[0], text="Concurrent replacement")
    f.clock[0] += timedelta(seconds=180)
    f.store.intent_status(f.slot, now=f.clock[0], observe=True)
    with f.app.intent_call_context(old_grace):
        with pytest.raises(AccessMeshError, match="intent_stale"):
            await f.app.resolve(actor(), ManagedOperation.COMMAND_RUN)
