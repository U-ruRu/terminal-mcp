"""Real managed session store behind the authenticated mesh application route."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.managed_sessions import ManagedSessionApplication, ManagedSlotGrant
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError
from terminal_mcp.application.session_gate import SessionGate
from terminal_mcp.core.managed_sessions import ManagedOperation, ManagedSessionError
from terminal_mcp.core.persistent_admission import current_admission_context
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.work_windows import WorkWindowStore


class Resolver:
    async def resolve(self, actor, provider, metadata):
        if provider != "openai" or metadata.get("openai/session") != "shared-conversation":
            raise ManagedSessionError("identity_not_bound")
        if actor.logical_agent_id not in (None, "la_one"):
            raise ManagedSessionError("identity_mismatch")
        return actor.with_agent("la_one", "home")


class Grants:
    async def ensure_agent_grant(self, actor, agent):
        assert actor.auth_mode == "oauth"
        assert actor.endpoint_role in {"executor", "coordinator"}

    async def authorize(self, actor, agent, operation):
        return ManagedSlotGrant(
            logical_agent_id=agent,
            authority_node_id="home",
            authority_epoch=1,
            public_name="Stable-1",
            principal_id=actor.principal_id,
            auth_generation=1,
        )


class Fence:
    async def revoke_session(self, *args, **kwargs):
        return []


class Link:
    def __init__(self, home):
        self.home = home
        self.payloads = []

    async def unified_session_call(self, node, operation, payload):
        assert node == "home"
        assert operation == "managed"
        admission = current_admission_context(required=True)
        body = {
            **payload,
            "requesting_instance_id": "peer",
            "forwarded_admission": {**asdict(admission), "scopes": sorted(admission.scopes)},
        }
        self.payloads.append(body)
        wire = await self.home.unified_session_stop(
            ActorContext(endpoint_role="mesh", peer_node_id="peer", node_id="home"),
            operation,
            body,
        )
        assert wire["ok"]
        return wire["result"]


def actor(principal="client-a", role="executor"):
    return ActorContext(
        principal_id=principal,
        credential_id="oauth:" + principal,
        auth_generation=1,
        auth_mode="oauth",
        scopes={"terminal:read", "terminal:execute"},
        node_id="peer",
        endpoint_role=role,
        provider="openai",
        provider_metadata={"openai/subject": "subject", "openai/session": "shared-conversation"},
    )


@pytest_asyncio.fixture
async def routed(tmp_path):
    repo = SqliteRepository(tmp_path / "home.sqlite3")
    await repo.initialize()
    store = WorkWindowStore(repo.path, authority_node_id="home")
    await store.create_slot(
        "la_one", "One", "ABCD", authority_node_id="home", initial_arm_duration_seconds=1380
    )
    managed = ManagedSessionApplication(store, Grants(), Fence())
    home_backend = SimpleNamespace(lifecycle=SimpleNamespace(authority_node_id="home"))
    gate = SessionGate(backend=home_backend, managed_identity=Resolver(), managed_sessions=managed)
    mesh = MeshApplication(
        SimpleNamespace(config=SimpleNamespace(instance_id="home")), home_backend, session_gate=gate
    )
    link = Link(mesh)
    peer_backend = SimpleNamespace(
        lifecycle=SimpleNamespace(authority_node_id="peer"), fleet_bridge=link
    )
    peer = SessionGate(backend=peer_backend, managed_identity=Resolver(), managed_sessions=object())
    return peer, store, link, mesh


@pytest.mark.asyncio
async def test_remote_start_authorize_end_and_role_successor_keep_identity(routed):
    gate, store, link, _ = routed
    first = await gate.start(actor(), mode=None)
    assert first["ok"], first
    identity, error = await gate.identity(actor(), None, ManagedOperation.COMMAND_RUN)
    assert error is None
    assert identity["logical_agent_id"] == "la_one"
    assert identity["authority_node_id"] == "home"
    assert identity["work_session_id"] == first["work_session_id"]
    assert identity["session_lifecycle"]["state"] == "active"
    assert (await gate.start(actor(), mode=None))["work_session_id"] == first["work_session_id"]
    ended = await gate.stop(actor(), None)
    assert ended["ok"] and not ended["stopping"], ended
    second = await gate.start(actor(role="coordinator"), mode=None)
    assert second["ok"], second
    assert second["session_epoch"] == first["session_epoch"] + 1
    assert (await store.active_session_for_slot("la_one")).work_session_id == second[
        "work_session_id"
    ]
    assert len(link.payloads) == 5


@pytest.mark.asyncio
async def test_remote_different_principal_and_live_role_conflict_are_preserved(routed):
    gate, store, _, _ = routed
    first = await gate.start(actor(), mode=None)
    conflict = await gate.start(actor(principal="other-client"), mode=None)
    assert conflict["code"] == "session_principal_mismatch", conflict
    role = await gate.start(actor(role="coordinator"), mode=None)
    assert role["code"] == "session_contract_conflict", role
    assert (await store.active_session_for_slot("la_one")).work_session_id == first[
        "work_session_id"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected",
    [
        ({"endpoint_role": "operator"}, "capability_not_allowed"),
        ({"logical_agent_id": "someone-else"}, "identity_mismatch"),
        ({"provider_metadata": {"openai/session": "unbound"}}, "identity_not_bound"),
        ({"action": "delete"}, "operation_not_allowed"),
        ({"forwarded_admission": {}}, "persistent_auth_required"),
    ],
)
async def test_home_route_rejects_untrusted_or_out_of_scope_forwarding(routed, change, expected):
    gate, _, link, mesh = routed
    await gate.start(actor(), mode=None)
    body = {**link.payloads[0], **change}
    result = await mesh.unified_session_managed(
        ActorContext(endpoint_role="mesh", peer_node_id="peer"), body
    )
    assert result["result"]["ok"] is False
    assert result["result"]["code"] == expected


@pytest.mark.asyncio
async def test_home_route_requires_authenticated_matching_peer(routed):
    _, _, _, mesh = routed
    with pytest.raises(MeshApplicationError, match="invalid fleet peer"):
        await mesh.unified_session_managed(actor(), {})
    with pytest.raises(MeshApplicationError, match="requesting instance mismatch"):
        await mesh.unified_session_managed(
            ActorContext(endpoint_role="mesh", peer_node_id="peer"),
            {"requesting_instance_id": "other"},
        )
