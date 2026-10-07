from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.session_gate import SessionGate
from terminal_mcp.core.managed_sessions import ManagedOperation, ManagedSessionError


class ProviderResolver:
    async def resolve(self, actor, provider, metadata):
        assert provider == "openai"
        assert metadata["openai/session"] == "conversation-1"
        return actor.with_agent("la_one", "node-a")


class ManagedSessions:
    def __init__(self, code: str, *, window_exists: bool):
        self.code = code
        self.window_exists = window_exists

    async def authorize_operation(self, actor, operation):
        raise ManagedSessionError(self.code, return_to_chat=self.code == "session_expired")

    async def start(self, actor):
        raise ManagedSessionError(self.code, return_to_chat=self.code == "session_expired")

    async def has_managed_window(self, logical_agent_id):
        assert logical_agent_id == "la_one"
        return self.window_exists


class LegacyBackend:
    def __init__(self):
        self.identity_calls = 0
        self.start_calls = 0
        self.lifecycle = SimpleNamespace(authority_node_id="node-a")

    async def access_identity(self, code):
        self.identity_calls += 1
        return {
            "ok": True,
            "logical_agent_id": "la_one",
            "work_session_id": "legacy-ws",
            "session_epoch": 1,
            "authority_node_id": "node-a",
        }

    async def access_session_start(self, **kwargs):
        self.start_calls += 1
        return {"ok": True, "session_ref": "legacy-ws", "mode": "persistent"}


def provider_actor():
    return ActorContext(
        provider="openai",
        provider_metadata={
            "openai/subject": "user-1",
            "openai/session": "conversation-1",
        },
    )


@pytest.mark.asyncio
async def test_legacy_code_cannot_bypass_managed_expired_window():
    backend = LegacyBackend()
    gate = SessionGate(
        backend=backend,
        managed_identity=ProviderResolver(),
        managed_sessions=ManagedSessions("session_expired", window_exists=True),
    )

    resolution = await gate.resolve(provider_actor(), "0042", ManagedOperation.COMMAND_RUN)

    assert resolution.failure == {
        "ok": False,
        "code": "session_expired",
        "error": "session_expired",
        "return_to_chat": True,
    }
    assert backend.identity_calls == 0


@pytest.mark.asyncio
async def test_grant_migration_can_use_legacy_code_only_before_managed_window_exists():
    backend = LegacyBackend()
    gate = SessionGate(
        backend=backend,
        managed_identity=ProviderResolver(),
        managed_sessions=ManagedSessions("access_denied", window_exists=False),
    )

    resolution = await gate.resolve(provider_actor(), "0042", ManagedOperation.COMMAND_RUN)

    assert resolution.failure is None
    assert resolution.identity["work_session_id"] == "legacy-ws"
    assert backend.identity_calls == 1


@pytest.mark.asyncio
async def test_revoked_managed_authorization_cannot_fall_back_after_window_exists():
    backend = LegacyBackend()
    gate = SessionGate(
        backend=backend,
        managed_identity=ProviderResolver(),
        managed_sessions=ManagedSessions("access_denied", window_exists=True),
    )

    resolution = await gate.resolve(provider_actor(), "0042", ManagedOperation.COMMAND_RUN)

    assert resolution.failure["code"] == "access_denied"
    assert backend.identity_calls == 0


@pytest.mark.asyncio
async def test_session_start_code_cannot_bypass_existing_managed_window():
    backend = LegacyBackend()
    gate = SessionGate(
        backend=backend,
        managed_identity=ProviderResolver(),
        managed_sessions=ManagedSessions("session_expired", window_exists=True),
    )

    result = await gate.start(provider_actor(), mode="persistent", code="0042")

    assert result["code"] == "session_expired"
    assert result["return_to_chat"] is True
    assert backend.start_calls == 0
