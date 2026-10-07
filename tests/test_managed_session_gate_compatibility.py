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


@pytest.mark.asyncio
async def test_role_session_bootstrap_binds_provider_once_without_public_access_code():
    class BootstrapResolver:
        def __init__(self):
            self.bound = False
            self.bind_calls = []

        async def resolve(self, actor, provider, metadata):
            if not self.bound:
                raise ManagedSessionError("identity_not_bound")
            return actor.with_agent("la_bootstrap", "node-a")

        async def bind_existing(
            self, actor, provider, metadata, logical_agent_id, *, access_code=None
        ):
            assert provider == "openai"
            assert metadata["openai/session"] == "conversation-1"
            assert logical_agent_id == "la_bootstrap"
            assert access_code == "0042"
            self.bound = True
            self.bind_calls.append(logical_agent_id)
            return actor.with_agent(logical_agent_id, "node-a")

    class BootstrapSessions:
        def __init__(self):
            self.grants = []
            self.starts = 0

        async def ensure_agent_grant(self, actor, logical_agent_id):
            self.grants.append(logical_agent_id)

        async def start(self, actor):
            self.starts += 1
            return SimpleNamespace(
                receipt=lambda: {
                    "public_name": "Alpha-1",
                    "work_session_id": "ws-bootstrap",
                    "session_epoch": 1,
                    "state": "active",
                    "started_at": "2026-10-07T00:00:00Z",
                    "hard_expires_at": "2026-10-07T01:00:00Z",
                    "remaining_seconds": 3600,
                    "role": "coordinator",
                    "contract_version": 1,
                }
            )

    class BootstrapBackend:
        def __init__(self):
            self.create_calls = 0

        async def slot_create(self, display_name):
            self.create_calls += 1
            assert display_name == "openai coordinator"
            return {
                "ok": True,
                "slot": {"logical_agent_id": "la_bootstrap"},
                "access": {"access_code": "0042"},
            }

    resolver = BootstrapResolver()
    sessions = BootstrapSessions()
    backend = BootstrapBackend()
    gate = SessionGate(backend=backend, managed_identity=resolver, managed_sessions=sessions)
    actor = ActorContext(
        principal_id="principal-1",
        credential_id="oauth:client-1",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        auth_mode="oauth",
        provider="openai",
        provider_metadata={
            "openai/subject": "user-1",
            "openai/session": "conversation-1",
        },
        endpoint_role="coordinator",
    )

    first = await gate.start(actor, mode="persistent", code=None)
    second = await gate.start(actor, mode="persistent", code=None)

    assert first["ok"] is second["ok"] is True
    assert first["public_name"] == second["public_name"] == "Alpha-1"
    assert first["session_ref"] == second["session_ref"] == "ws-bootstrap"
    assert "access_code" not in first
    assert backend.create_calls == 1
    assert resolver.bind_calls == ["la_bootstrap"]
    assert sessions.grants == ["la_bootstrap", "la_bootstrap"]
    assert sessions.starts == 2


@pytest.mark.asyncio
async def test_valid_access_code_stop_bypasses_unrelated_provider_resolution():
    class Resolver:
        async def resolve(self, *args, **kwargs):
            raise AssertionError("provider identity must not override explicit access permission")

    class Sessions:
        pass

    class Backend:
        lifecycle = SimpleNamespace(authority_node_id="node-a")

        def __init__(self):
            self.calls = []

        async def access_session_stop(self, code, *, interrupt=False, _resolved_access=None):
            self.calls.append((code, interrupt, _resolved_access))
            return {"ok": True, "session_ref": "legacy-ws", "stopping": False}

    backend = Backend()
    gate = SessionGate(backend=backend, managed_identity=Resolver(), managed_sessions=Sessions())
    result = await gate.stop(
        provider_actor(),
        "0042",
        interrupt=True,
        resolved_access={"authority_node_id": "node-b", "logical_agent_id": "legacy-agent"},
    )

    assert result["ok"] is True
    assert backend.calls == [
        (
            "0042",
            True,
            {"authority_node_id": "node-b", "logical_agent_id": "legacy-agent"},
        )
    ]
