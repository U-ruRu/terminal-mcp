"""Access start selection with storage, authorization and mutation ports doubled."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    SessionSnapshot,
    SlotPolicy,
    SlotSnapshot,
)

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
NUMBER = "0427"
BINDING = "a" * 64


@pytest.fixture
def access_actor():
    return ActorContext(principal_id="caller", endpoint_role="access")


@pytest.fixture
def slot():
    started = NOW - timedelta(hours=2)
    ended = NOW - timedelta(hours=1)
    return SlotSnapshot(
        issuer_id="issuer",
        slot_id="existing-slot",
        session=SessionSnapshot("existing-agent", False, ended, started),
        kind="persistent",
        state="active",
        revision=7,
        policy=SlotPolicy(duration_seconds=3600, cooldown_seconds=60, rearm_enabled=False),
        anchor=started,
        deadline_at=ended,
        code_tag="fixture-code-tag",
    )


@pytest.fixture
def app():
    store = SimpleNamespace(
        namespace_id="test-registry",
        local_node_id="issuer",
        trusted_issuers=frozenset({"issuer"}),
        defaults=Mock(return_value=None),
        registry_code_slot=Mock(return_value=None),
        issuer_bound_slot=Mock(return_value=None),
        registry_slot=Mock(return_value=None),
        numbers=SimpleNamespace(number_for_slot=Mock(return_value=NUMBER)),
        human_name=Mock(return_value="Alfa"),
        prepare_receipt=Mock(side_effect=lambda **values: values),
        save_receipt=Mock(),
        touch_slot=Mock(),
    )
    application = AccessMeshApplication(
        store, task_store=Mock(), execution_fence=Mock(), clock=lambda: NOW
    )
    application.require_write = Mock()
    application.connection_key = Mock(return_value=BINDING)
    application.proxy_registry = AsyncMock(return_value=None)
    application.defaults_state = Mock(return_value={"legacy_admission_enabled": False})
    application.issue = AsyncMock(return_value={"ok": True, "access_code": "9081"})
    application.change = AsyncMock(return_value={"ok": True})
    return application


@pytest.mark.asyncio
async def test_explicit_existing_number_starts_without_legacy_or_rearm(app, slot, access_actor):
    app.store.registry_code_slot.return_value = slot

    result = await app.issuer_session(access_actor, action="start", session_number=NUMBER)

    assert result == {"ok": True, "session_number": NUMBER}
    app.store.registry_code_slot.assert_called_once_with(NUMBER)
    app.change.assert_awaited_once_with(
        access_actor,
        slot_id=slot.slot_id,
        kind="SessionStarted",
        expected_revision=slot.revision,
        receipt_spec=ANY,
        effective_at=ANY,
        deadline_at=ANY,
        number_start=ANY,
    )
    app.defaults_state.assert_not_called()
    app.issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_without_number_keeps_bound_active_session(app, slot, access_actor):
    started = NOW - timedelta(minutes=5)
    active = replace(
        slot,
        session=replace(slot.session, active=True, ended_at=None, active_from=started),
        anchor=started,
        deadline_at=NOW + timedelta(minutes=55),
    )
    app.store.issuer_bound_slot.return_value = active

    result = await app.issuer_session(access_actor, action="start")

    assert result == {"ok": True, "session_number": NUMBER}
    app.store.issuer_bound_slot.assert_called_once_with(BINDING)
    app.store.registry_code_slot.assert_not_called()
    app.store.numbers.number_for_slot.assert_called_once_with(active.issuer_id, active.slot_id)
    app.store.save_receipt.assert_called_once()
    receipt = app.store.save_receipt.call_args.args[0]
    assert receipt["connection_key"] == BINDING
    assert receipt["result"]["slot_id"] == active.slot_id
    app.change.assert_not_awaited()
    app.issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_binding_cannot_issue_when_legacy_is_disabled(app, access_actor):
    with pytest.raises(AccessMeshError, match="^legacy_admission_disabled$"):
        await app.issuer_session(access_actor, action="start")

    app.issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_binding_issues_when_legacy_is_enabled(app, access_actor):
    app.defaults_state.return_value = {"legacy_admission_enabled": True}

    result = await app.issuer_session(access_actor, action="start")

    assert result == {"ok": True, "session_number": "9081"}
    app.issue.assert_awaited_once_with(access_actor, kind="temporary", receipt_spec=ANY)


@pytest.mark.asyncio
async def test_unknown_explicit_number_cannot_fall_back_to_legacy_issuance(app, access_actor):
    app.defaults_state.return_value = {"legacy_admission_enabled": True}

    with pytest.raises(AccessMeshError, match="^invalid_session_number$"):
        await app.issuer_session(access_actor, action="start", session_number="9999")

    app.store.registry_code_slot.assert_called_once_with("9999")
    app.defaults_state.assert_not_called()
    app.issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_start_proxies_immutable_slot_id(app, slot, access_actor):
    executor = replace(access_actor, endpoint_role="executor")
    app.proxy_registry.return_value = {"ok": True, "session_number": NUMBER}

    result = await app.issuer_session(
        executor, action="start", automatic=True, slot_id=slot.slot_id
    )

    assert result == {"ok": True, "session_number": NUMBER}
    app.proxy_registry.assert_awaited_once_with(
        executor, "session", {"action": "start", "automatic": True, "slot_id": slot.slot_id}
    )
    app.store.registry_code_slot.assert_not_called()
    app.issue.assert_not_awaited()
