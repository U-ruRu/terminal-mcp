from __future__ import annotations

import base64
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.http.persistent_fleet import build_persistent_fleet_router
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


def _enc(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    return (
        _enc(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        _enc(
            key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ),
    )


def _admission() -> VerifiedAdmissionContext:
    return VerifiedAdmissionContext(
        principal_id="connector-client",
        credential_id="oauth:connector-client",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )


def _service(repo: SqliteRepository):
    return SimpleNamespace(
        repo=repo,
        terminal=object(),
        task_coordinator=object(),
        task_store=TaskStore(repo.path),
        events=None,
    )


class _ReplicationPeerAuth:
    def __init__(self, *, peer_id: str, token: str):
        self.peer_id = peer_id
        self.token = token

    def authenticate(self, peer_id: str, authorization: str):
        if peer_id != self.peer_id or authorization != f"Bearer {self.token}":
            return None
        return SimpleNamespace(instance_id=peer_id)


@pytest.mark.asyncio
async def test_remote_access_identity_tracks_authority_session_across_end_and_reopen(tmp_path):
    ctx = _admission()
    t0 = utc_now()

    authority_repo = SqliteRepository(
        tmp_path / "authority-runtime.sqlite3",
        tmp_path / "authority-output.sqlite3",
    )
    await authority_repo.initialize()
    authority_store = PersistentAgentStore(authority_repo.path)
    authority_lifecycle = PersistentLifecycleCoordinator(
        authority_store,
        enabled=True,
        authority_node_id="bacloud",
        session_duration_seconds=1200,
        rearm_delay_seconds=180,
    )

    created = await authority_lifecycle.create_slot("Sender", admission=ctx)
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await authority_lifecycle.play(
        logical_agent_id,
        expected_revision=created["slot"]["slot_revision"],
        admission=ctx,
    )
    first = await authority_lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
        origin_instance_id="bacloud",
        now=utc_text(t0),
    )

    authority_auth = AuthFoundationStore(tmp_path / "authority-auth.sqlite3")
    await authority_auth.initialize()
    await authority_auth.register_access_slot(
        logical_agent_id,
        "bacloud",
        display_suffix="Sender",
    )
    await authority_auth.issue_access_code(logical_agent_id, requested_code="0042")

    caller_auth = AuthFoundationStore(tmp_path / "caller-auth.sqlite3")
    await caller_auth.initialize()
    await caller_auth.register_access_slot(
        logical_agent_id,
        "bacloud",
        display_suffix="Sender",
    )
    await caller_auth.issue_access_code(logical_agent_id, requested_code="0042")

    main_private, main_public = _keypair()
    bacloud_private, bacloud_public = _keypair()

    authority_config = FleetConfig(
        "bacloud",
        bacloud_private,
        (FleetPeer("main", "https://main.example", main_public, "main-token"),),
        1.0,
        1.0,
        "bacloud-token",
    )
    authority_bridge = PersistentFleetBridge(
        authority_config,
        authority_store,
        authority_repo,
        object(),
        TaskStore(authority_repo.path),
    )
    authority_backend = PersistentBackend(
        _service(authority_repo),
        authority_lifecycle,
        access_authority=authority_auth,
    )

    authority_app = FastAPI()
    authority_app.include_router(
        build_persistent_fleet_router(
            _ReplicationPeerAuth(peer_id="main", token="main-token"),
            authority_bridge,
            authority_backend,
        )
    )

    caller_repo = SqliteRepository(
        tmp_path / "caller-runtime.sqlite3",
        tmp_path / "caller-output.sqlite3",
    )
    await caller_repo.initialize()
    caller_store = PersistentAgentStore(caller_repo.path)
    caller_lifecycle = PersistentLifecycleCoordinator(
        caller_store,
        enabled=True,
        authority_node_id="main",
        session_duration_seconds=1200,
        rearm_delay_seconds=180,
    )
    caller_config = FleetConfig(
        "main",
        main_private,
        (FleetPeer("bacloud", "https://bacloud.example", bacloud_public, "unused"),),
        1.0,
        1.0,
        "main-token",
    )

    def authority_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=authority_app),
            timeout=1.0,
        )

    caller_control = FleetControlStore(
        tmp_path / "caller-fleet-control.sqlite3",
        fleet_id="fleet-a",
        node_id="main",
        control_node_id="main",
    )
    await caller_control.initialize()
    caller_bridge = PersistentFleetBridge(
        caller_config,
        caller_store,
        caller_repo,
        object(),
        TaskStore(caller_repo.path),
        client_factory=authority_client,
        control_store=caller_control,
        control_node_id="main",
        access_authority=caller_auth,
    )
    caller_backend = PersistentBackend(
        _service(caller_repo),
        caller_lifecycle,
        caller_bridge,
        access_authority=caller_auth,
    )

    active = await caller_backend.access_identity("0042")

    assert active["ok"] is True
    assert active["logical_agent_id"] == logical_agent_id
    assert active["work_session_id"] == first["work_session_id"]
    assert active["session_epoch"] == first["session_epoch"]
    route = await caller_bridge.route_info(logical_agent_id)
    assert route is not None
    assert route["authority_node_id"] == "bacloud"
    assert route["authority_epoch"] == 1
    assert (
        await caller_bridge.inbox_obligations(
            logical_agent_id,
            work_session_id=first["work_session_id"],
            session_epoch=first["session_epoch"],
        )
        == []
    )

    ended = await authority_lifecycle.session_end(
        logical_agent_id,
        first["work_session_id"],
        first["session_epoch"],
        admission=ctx,
        now=utc_text(t0 + timedelta(seconds=5)),
    )

    fenced = await caller_backend.access_identity("0042")

    assert fenced == {
        "ok": False,
        "code": "session_not_found",
        "error": "session_not_found",
    }

    reopened = await authority_lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=ended["slot"]["slot_revision"],
        admission=ctx,
        origin_instance_id="main",
        now=utc_text(t0 + timedelta(seconds=6)),
    )
    assert reopened["session_epoch"] == first["session_epoch"] + 1

    resumed = await caller_backend.access_identity("0042")

    assert resumed["ok"] is True
    assert resumed["logical_agent_id"] == logical_agent_id
    assert resumed["work_session_id"] == reopened["work_session_id"]
    assert resumed["session_epoch"] == reopened["session_epoch"]


@pytest.mark.asyncio
async def test_verified_access_code_authorizes_local_session_without_transport_admission(tmp_path):
    repo = SqliteRepository(
        tmp_path / "runtime.sqlite3",
        tmp_path / "output.sqlite3",
    )
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    lifecycle = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="bacloud",
        session_duration_seconds=1200,
    )
    ctx = _admission()
    created = await lifecycle.create_slot("Sender", admission=ctx)
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await lifecycle.play(
        logical_agent_id,
        expected_revision=created["slot"]["slot_revision"],
        admission=ctx,
    )
    started = await lifecycle.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
    )

    session = await lifecycle.authorize_session(
        logical_agent_id,
        started["work_session_id"],
        started["session_epoch"],
        access_code_verified=True,
    )

    assert session.logical_agent_id == logical_agent_id
    assert session.work_session_id == started["work_session_id"]
