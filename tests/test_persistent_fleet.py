import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.core.orchestration import parse_utc
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge, verify_permit
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


def enc(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def keypair():
    key = Ed25519PrivateKey.generate()
    return (
        enc(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        enc(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
    )


def admission():
    return VerifiedAdmissionContext(
        principal_id="client-1",
        credential_id="oauth:client-1",
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="oauth",
    )


@pytest.mark.asyncio
async def test_signed_permit_is_exact_node_bound_and_tamper_evident(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    life = PersistentLifecycleCoordinator(
        store, enabled=True, authority_node_id="bacloud", session_duration_seconds=120
    )
    ctx = admission()
    created = await life.create_slot("Alpha", admission=ctx)
    armed = await life.play(
        created["slot"]["logical_agent_id"],
        expected_revision=created["slot"]["slot_revision"],
        admission=ctx,
    )
    started = await life.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
        origin_instance_id="bacloud",
    )
    bc_priv, bc_pub = keypair()
    _, fb_pub = keypair()
    cfg = FleetConfig(
        "bacloud",
        bc_priv,
        (FleetPeer("firstbyte", "https://firstbyte.example", fb_pub, "fb-token"),),
        1.0,
        1.0,
    )
    bridge = PersistentFleetBridge(cfg, store, repo, object(), TaskStore(repo.path))
    ws = started["work_session"]
    permit = await bridge.issue_permit(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="firstbyte",
        scope="run",
        principal_id=ctx.principal_id,
    )
    assert permit.node_instance_id == "firstbyte"
    assert permit.authority_node_id == "bacloud"
    assert verify_permit(permit, bc_pub)
    assert not verify_permit(replace(permit, scope="cancel"), bc_pub)
    assert parse_utc(permit.permit_expires_at) <= parse_utc(permit.hard_expires_at)
    attachments = await store.attachments_for_session(
        permit.logical_agent_id, permit.work_session_id, permit.session_epoch
    )
    assert [item["node_instance_id"] for item in attachments] == ["firstbyte"]
    with pytest.raises(PersistentStoreError, match="persistent_auth_required"):
        await bridge.issue_permit(
            logical_agent_id=permit.logical_agent_id,
            work_session_id=permit.work_session_id,
            session_epoch=permit.session_epoch,
            requesting_instance_id="firstbyte",
            scope="run",
            principal_id="wrong-client",
        )


class Resp:
    status_code = 200

    def __init__(self, permit):
        self.permit = permit

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "permit": self.permit.as_dict()}


class Client:
    def __init__(self, permit):
        self.permit = permit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return Resp(self.permit)


@pytest.mark.asyncio
async def test_remote_accepts_only_exact_peer_signed_permit(tmp_path):
    # Create authority-side permit using the first test's real lifecycle path.
    repo = SqliteRepository(tmp_path / "auth.sqlite3", tmp_path / "auth-out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    life = PersistentLifecycleCoordinator(
        store, enabled=True, authority_node_id="bacloud", session_duration_seconds=120
    )
    ctx = admission()
    created = await life.create_slot("Alpha", admission=ctx)
    armed = await life.play(created["slot"]["logical_agent_id"], expected_revision=1, admission=ctx)
    started = await life.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
    )
    bc_priv, bc_pub = keypair()
    fb_priv, fb_pub = keypair()
    authority = PersistentFleetBridge(
        FleetConfig(
            "bacloud", bc_priv, (FleetPeer("firstbyte", "https://fb", fb_pub, "t"),), 1.0, 1.0
        ),
        store,
        repo,
        object(),
        TaskStore(repo.path),
    )
    ws = started["work_session"]
    permit = await authority.issue_permit(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="firstbyte",
        scope="run",
        principal_id=ctx.principal_id,
    )
    rrepo = SqliteRepository(tmp_path / "remote.sqlite3", tmp_path / "remote-out.sqlite3")
    await rrepo.initialize()
    rstore = PersistentAgentStore(rrepo.path)
    rcfg = FleetConfig(
        "firstbyte", fb_priv, (FleetPeer("bacloud", "https://bc", bc_pub, "t"),), 1.0, 1.0
    )
    remote = PersistentFleetBridge(
        rcfg, rstore, rrepo, object(), TaskStore(rrepo.path), client_factory=lambda: Client(permit)
    )
    accepted = await remote.acquire_permit(
        logical_agent_id=permit.logical_agent_id,
        work_session_id=permit.work_session_id,
        session_epoch=permit.session_epoch,
        scope="run",
        principal_id=ctx.principal_id,
    )
    assert accepted == permit
    remote.client_factory = lambda: Client(replace(permit, scope="cancel"))
    with pytest.raises(PersistentStoreError, match="authority_unavailable"):
        await remote.acquire_permit(
            logical_agent_id=permit.logical_agent_id,
            work_session_id=permit.work_session_id,
            session_epoch=permit.session_epoch,
            scope="run",
            principal_id=ctx.principal_id,
        )


class BlockingFence:
    async def revoke_session(self, logical_agent_id, work_session_id, session_epoch, *, reason):
        return [{"kind": "running_command", "command_hash": "deadbeef"}]


@pytest.mark.asyncio
async def test_remote_revoke_fences_permits_before_reporting_running_blocker(tmp_path):
    repo = SqliteRepository(
        tmp_path / "remote-revoke.sqlite3", tmp_path / "remote-revoke-out.sqlite3"
    )
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    private_key, _public_key = keypair()
    _, authority_public = keypair()
    config = FleetConfig(
        "firstbyte",
        private_key,
        (FleetPeer("bacloud", "https://bc", authority_public, "t"),),
        1.0,
        1.0,
    )
    bridge = PersistentFleetBridge(config, store, repo, object(), TaskStore(repo.path))
    bridge.execution_fence = BlockingFence()
    now = "2026-09-30T11:00:00.000000Z"
    permit = {
        "logical_agent_id": "logical-1",
        "work_session_id": "ws-1",
        "session_epoch": 3,
        "authority_node_id": "bacloud",
        "authority_epoch": 2,
        "node_attachment_id": "att-1",
        "node_instance_id": "firstbyte",
        "scope": "run",
        "hard_expires_at": "2026-09-30T11:20:00.000000Z",
        "permit_expires_at": "2026-09-30T11:10:00.000000Z",
        "signature": "sig",
        "issued_at": now,
    }
    await store.record_command_permit("deadbeef", permit)

    blockers = await bridge.receive_revoke(
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=3,
        reason="suspend",
    )
    assert blockers == [{"kind": "running_command", "command_hash": "deadbeef"}]

    import sqlite3

    with sqlite3.connect(repo.path) as db:
        revoked_at = db.execute(
            "SELECT revoked_at FROM persistent_command_permits WHERE command_hash='deadbeef'"
        ).fetchone()[0]
    assert revoked_at is not None
