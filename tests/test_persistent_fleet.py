import asyncio
import base64
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.core.persistent_execution import PersistentExecutionFence
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge, verify_permit
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


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

    async def resolve_access_code(code):
        assert code == "0042"
        return {
            "logical_agent_id": permit.logical_agent_id,
            "authority_node_id": "bacloud",
            "access_generation": 3,
        }

    bridge.resolve_access_code = resolve_access_code
    cross_connector = await bridge.issue_permit(
        logical_agent_id=permit.logical_agent_id,
        work_session_id=permit.work_session_id,
        session_epoch=permit.session_epoch,
        requesting_instance_id="firstbyte",
        scope="run",
        principal_id="connector-on-firstbyte",
        access_code="0042",
    )
    assert cross_connector.principal_id == "connector-on-firstbyte"
    assert verify_permit(cross_connector, bc_pub)


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


class DelayedClient:
    def __init__(self, permit, entered, release):
        self.permit = permit
        self.entered = entered
        self.release = release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        return Resp(self.permit)


class DrainFence:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def blockers_for_session(self, logical_agent_id, work_session_id, session_epoch):
        self.entered.set()
        await self.release.wait()
        return []


@pytest.mark.asyncio
async def test_delayed_remote_permit_loses_race_to_graceful_drain(tmp_path):
    repo = SqliteRepository(tmp_path / "race-auth.sqlite3", tmp_path / "race-auth-out.sqlite3")
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

    rrepo = SqliteRepository(tmp_path / "race-remote.sqlite3", tmp_path / "race-remote-out.sqlite3")
    await rrepo.initialize()
    rstore = PersistentAgentStore(rrepo.path)
    response_entered = asyncio.Event()
    response_release = asyncio.Event()
    drain_fence = DrainFence()
    remote = PersistentFleetBridge(
        FleetConfig(
            "firstbyte", fb_priv, (FleetPeer("bacloud", "https://bc", bc_pub, "t"),), 1.0, 1.0
        ),
        rstore,
        rrepo,
        object(),
        TaskStore(rrepo.path),
        client_factory=lambda: DelayedClient(permit, response_entered, response_release),
    )
    remote.execution_fence = drain_fence

    acquiring = asyncio.create_task(
        remote.acquire_permit(
            logical_agent_id=permit.logical_agent_id,
            work_session_id=permit.work_session_id,
            session_epoch=permit.session_epoch,
            scope="run",
            principal_id=ctx.principal_id,
        )
    )
    await response_entered.wait()
    draining = asyncio.create_task(
        remote.receive_drain(
            logical_agent_id=permit.logical_agent_id,
            work_session_id=permit.work_session_id,
            session_epoch=permit.session_epoch,
            hard_expires_at=permit.hard_expires_at,
        )
    )
    await drain_fence.entered.wait()
    response_release.set()
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await acquiring
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        remote.ensure_permit_valid(permit)
    drain_fence.release.set()
    assert await draining == []


@pytest.mark.asyncio
async def test_authority_rechecks_session_after_attachment_before_returning_permit(tmp_path):
    repo = SqliteRepository(tmp_path / "recheck.sqlite3", tmp_path / "recheck-out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    life = PersistentLifecycleCoordinator(
        store, enabled=True, authority_node_id="bacloud", session_duration_seconds=120
    )
    ctx = admission()
    created = await life.create_slot("Alpha", admission=ctx)
    logical_agent_id = created["slot"]["logical_agent_id"]
    armed = await life.play(logical_agent_id, expected_revision=1, admission=ctx)
    started = await life.session_start(
        created["selector"]["selector"],
        expected_revision=armed["slot"]["slot_revision"],
        admission=ctx,
    )
    ws = started["work_session"]
    bc_priv, _bc_pub = keypair()
    _, fb_pub = keypair()
    bridge = PersistentFleetBridge(
        FleetConfig(
            "bacloud", bc_priv, (FleetPeer("firstbyte", "https://fb", fb_pub, "t"),), 1.0, 1.0
        ),
        store,
        repo,
        object(),
        TaskStore(repo.path),
    )
    original_record = store.record_node_attachment

    async def record_then_stop(**kwargs):
        attachment = await original_record(**kwargs)
        await life.session_end(
            logical_agent_id,
            ws["work_session_id"],
            ws["session_epoch"],
            admission=ctx,
        )
        return attachment

    store.record_node_attachment = record_then_stop
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await bridge.issue_permit(
            logical_agent_id=logical_agent_id,
            work_session_id=ws["work_session_id"],
            session_epoch=ws["session_epoch"],
            requesting_instance_id="firstbyte",
            scope="run",
            principal_id=ctx.principal_id,
        )
    assert (
        await store.attachments_for_session(
            logical_agent_id, ws["work_session_id"], ws["session_epoch"], active_only=True
        )
        == []
    )


@pytest.mark.asyncio
async def test_remote_drain_preserves_materialized_command_until_natural_completion(
    tmp_path,
):
    repo = SqliteRepository(tmp_path / "drain-work.sqlite3", tmp_path / "drain-work-out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    private_key, _public_key = keypair()
    _, authority_public = keypair()
    bridge = PersistentFleetBridge(
        FleetConfig(
            "firstbyte",
            private_key,
            (FleetPeer("bacloud", "https://bc", authority_public, "t"),),
            1.0,
            1.0,
        ),
        store,
        repo,
        terminal,
        TaskStore(repo.path),
    )
    bridge.execution_fence = PersistentExecutionFence(repo, terminal, TaskStore(repo.path))
    now = utc_now()
    hard_expires_at = utc_text(now + timedelta(minutes=5))
    permit_expires_at = utc_text(now + timedelta(seconds=30))
    command = await repo.create(
        "printf admitted",
        queue_id=1,
        agent_id="logical-1",
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=3,
        command_type="persistent_run",
        persistent_permit={
            "logical_agent_id": "logical-1",
            "work_session_id": "ws-1",
            "session_epoch": 3,
            "authority_node_id": "bacloud",
            "authority_epoch": 2,
            "node_attachment_id": "att-1",
            "node_instance_id": "firstbyte",
            "scope": "run",
            "hard_expires_at": hard_expires_at,
            "permit_expires_at": permit_expires_at,
            "signature": "sig-existing",
            "issued_at": utc_text(now),
            "gate_revision": 1,
            "operation": "run",
            "ttl_ms": 30000,
            "slot_revision": 2,
            "principal_id": "client-1",
        },
    )

    blockers = await bridge.receive_drain(
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=3,
        hard_expires_at=hard_expires_at,
    )
    assert {item["command_hash"] for item in blockers} == {command.cmd_hash}
    assert (await repo.get(command.cmd_hash)).status == "queued"

    claimed = await repo.claim_next(1)
    assert claimed is not None and claimed.cmd_hash == command.cmd_hash
    assert await repo.finish_running(command.cmd_hash, "completed", 0, None)
    assert (await repo.get(command.cmd_hash)).status == "completed"


@pytest.mark.asyncio
async def test_access_list_falls_back_to_legacy_get_when_control_node_lacks_list_route(tmp_path):
    repo = SqliteRepository(tmp_path / "compat.sqlite3", tmp_path / "compat-out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    life = PersistentLifecycleCoordinator(
        store, enabled=True, authority_node_id="bacloud", session_duration_seconds=120
    )
    created = await life.create_slot("Compat", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    private_key, _ = keypair()
    _, control_public = keypair()
    config = FleetConfig(
        "bacloud",
        private_key,
        (FleetPeer("main", "https://main.example", control_public, "token"),),
        1.0,
        1.0,
    )
    bridge = PersistentFleetBridge(
        config,
        store,
        repo,
        object(),
        TaskStore(repo.path),
        control_node_id="main",
    )
    calls = []

    async def remote_call(operation, payload):
        calls.append((operation, payload))
        if operation == "list":
            raise PersistentStoreError("route_unavailable")
        assert operation == "get"
        assert payload == {"logical_agent_id": logical_agent_id}
        return {
            "ok": True,
            "access": {
                "logical_agent_id": logical_agent_id,
                "authority_node_id": "bacloud",
                "slot_kind": "persistent",
                "public_name": "Alpha-Compat",
                "display_suffix": "Compat",
                "access_generation": 1,
            },
        }

    bridge._remote_access_call = remote_call
    result = await bridge.list_access_slots()
    assert [item["logical_agent_id"] for item in result] == [logical_agent_id]
    assert [operation for operation, _ in calls] == ["list", "get"]


@pytest.mark.asyncio
async def test_access_list_does_not_mask_real_authority_failure(tmp_path):
    repo = SqliteRepository(tmp_path / "compat-fail.sqlite3", tmp_path / "compat-fail-out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    private_key, _ = keypair()
    _, control_public = keypair()
    bridge = PersistentFleetBridge(
        FleetConfig(
            "bacloud",
            private_key,
            (FleetPeer("main", "https://main.example", control_public, "token"),),
            1.0,
            1.0,
        ),
        store,
        repo,
        object(),
        TaskStore(repo.path),
        control_node_id="main",
    )

    async def remote_call(operation, payload):
        raise PersistentStoreError("authority_unavailable")

    bridge._remote_access_call = remote_call
    with pytest.raises(PersistentStoreError, match="authority_unavailable"):
        await bridge.list_access_slots()


@pytest.mark.asyncio
async def test_access_by_name_falls_back_to_legacy_get_when_control_node_lacks_route(
    tmp_path,
):
    repo = SqliteRepository(tmp_path / "compat-name.sqlite3", tmp_path / "compat-name-out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    life = PersistentLifecycleCoordinator(
        store, enabled=True, authority_node_id="bacloud", session_duration_seconds=120
    )
    created = await life.create_slot("CompatName", admission=admission())
    logical_agent_id = created["slot"]["logical_agent_id"]
    private_key, _ = keypair()
    _, control_public = keypair()
    bridge = PersistentFleetBridge(
        FleetConfig(
            "bacloud",
            private_key,
            (FleetPeer("main", "https://main.example", control_public, "token"),),
            1.0,
            1.0,
        ),
        store,
        repo,
        object(),
        TaskStore(repo.path),
        control_node_id="main",
    )
    calls = []

    async def remote_call(operation, payload):
        calls.append((operation, payload))
        if operation == "by-name":
            raise PersistentStoreError("route_unavailable")
        assert operation == "get"
        return {
            "ok": True,
            "access": {
                "logical_agent_id": logical_agent_id,
                "authority_node_id": "bacloud",
                "slot_kind": "persistent",
                "public_name": "Alpha-CompatName",
                "display_suffix": "CompatName",
                "access_generation": 1,
            },
        }

    bridge._remote_access_call = remote_call
    result = await bridge.get_access_slot_by_public_name("Alpha-CompatName")
    assert result["logical_agent_id"] == logical_agent_id
    assert [operation for operation, _ in calls] == ["by-name", "get"]


@pytest.mark.asyncio
async def test_access_by_name_does_not_mask_real_authority_failure(tmp_path):
    repo = SqliteRepository(
        tmp_path / "compat-name-fail.sqlite3", tmp_path / "compat-name-fail-out.sqlite3"
    )
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    private_key, _ = keypair()
    _, control_public = keypair()
    bridge = PersistentFleetBridge(
        FleetConfig(
            "bacloud",
            private_key,
            (FleetPeer("main", "https://main.example", control_public, "token"),),
            1.0,
            1.0,
        ),
        store,
        repo,
        object(),
        TaskStore(repo.path),
        control_node_id="main",
    )

    async def remote_call(operation, payload):
        raise PersistentStoreError("authority_unavailable")

    bridge._remote_access_call = remote_call
    with pytest.raises(PersistentStoreError, match="authority_unavailable"):
        await bridge.get_access_slot_by_public_name("Alpha-CompatName")
