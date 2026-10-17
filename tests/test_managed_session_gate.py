from datetime import UTC, datetime
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


def provider_actor():
    return ActorContext(
        provider="openai",
        provider_metadata={
            "openai/subject": "user-1",
            "openai/session": "conversation-1",
        },
    )


@pytest.mark.asyncio
async def test_role_session_bootstrap_binds_authenticated_provider():
    class BootstrapResolver:
        def __init__(self):
            self.bound = False
            self.bind_calls = []

        async def resolve(self, actor, provider, metadata):
            if not self.bound:
                raise ManagedSessionError("identity_not_bound")
            return actor.with_agent("la_bootstrap", "node-a")

        async def bind_existing(
            self,
            actor,
            provider,
            metadata,
            logical_agent_id,
            *,
            access_code=None,
            bootstrap=False,
        ):
            assert provider == "openai"
            assert metadata["openai/session"] == "conversation-1"
            assert logical_agent_id == "la_bootstrap"
            assert access_code is None
            assert bootstrap is True
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

    assert first["ok"]
    assert first["public_name"] == "Alpha-1"
    assert first["session_ref"] == "ws-bootstrap"
    assert backend.create_calls == 1
    assert resolver.bind_calls == ["la_bootstrap"]
    assert sessions.grants == ["la_bootstrap"]
    assert sessions.starts == 1


@pytest.mark.asyncio
async def test_current_state_serializes_persisted_string_session_state():
    class Sessions:
        async def authorize_operation(self, actor, operation):
            assert operation is ManagedOperation.OBSERVE
            now = datetime(2026, 10, 7, tzinfo=UTC)
            return SimpleNamespace(
                public_name="Alpha-1",
                lifecycle=SimpleNamespace(
                    phase=SimpleNamespace(value="active"), remaining_seconds=1200
                ),
                snapshot=SimpleNamespace(
                    window=SimpleNamespace(
                        work_window_id="ww-1",
                        opened_at=now,
                        hard_expires_at=now,
                        window_revision=3,
                    ),
                    session=SimpleNamespace(
                        logical_agent_id="la_one",
                        authority_node_id="node-a",
                        work_session_id="ws-1",
                        session_epoch=2,
                        state="active",
                        started_at="2026-10-07T00:00:00Z",
                        hard_expires_at="2026-10-07T00:23:00Z",
                    ),
                    binding=SimpleNamespace(role="coordinator", contract_version=1),
                ),
            )

    gate = SessionGate(managed_identity=ProviderResolver(), managed_sessions=Sessions())
    result = await gate.current_state(provider_actor())

    assert result["ok"] is True
    assert result["logical_agent"]["public_name"] == "Alpha-1"
    assert result["work_session"]["state"] == "active"
    assert result["work_session"]["work_session_id"] == "ws-1"


@pytest.mark.asyncio
async def test_unbound_provider_on_session_bound_tool_requires_session_start():
    gate = SessionGate(
        backend=SimpleNamespace(lifecycle=SimpleNamespace(authority_node_id="node-a")),
        managed_identity=ProviderResolver(),
        managed_sessions=ManagedSessions("identity_not_bound", window_exists=False),
    )

    resolution = await gate.resolve(provider_actor(), None, ManagedOperation.TASK_LIST)

    assert resolution.failure == {
        "ok": False,
        "code": "session_required",
        "error": "session_required",
    }
