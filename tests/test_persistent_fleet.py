import base64
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.human_names import number_agent_name
from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.core.persistent_execution import PersistentExecutionFence
from terminal_mcp.core.persistent_fleet import (
    PersistentCommandPermit,
    PersistentFleetBridge,
    sign_permit,
    verify_permit,
)
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.access_mesh import AccessMeshStore
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


@pytest_asyncio.fixture
async def native_authority(tmp_path):
    repo = SqliteRepository(tmp_path / "native-authority.sqlite3")
    await repo.initialize()
    now = utc_now()
    mesh = AccessMeshStore(
        repo.path,
        local_node_id="bacloud",
        trusted_issuers=frozenset({"bacloud", "firstbyte"}),
        proof_key=b"current-permit-fixture-proof-key-32bytes",
    )
    mesh.apply_event(
        AccessSlotEvent(
            issuer_id="bacloud",
            slot_id="permit-slot",
            logical_agent_id="la_permit",
            revision=1,
            event_id="bacloud:permit-slot:1",
            kind="SlotIssued",
            policy=SlotPolicy(120, 30),
            code_tag=mesh.code_tag("bacloud", "1234"),
            effective_at=now,
            agent_name=number_agent_name("1234"),
        ),
        authenticated_peer_id="bacloud",
        issued_kind="persistent",
        mutation_number={"number": "1234", "attempt_id": "fixture", "started_at": now},
    )
    identity = mesh.local_identity(mesh.slot("bacloud", "permit-slot"), now=now)
    store = PersistentAgentStore(repo.path)
    private, public = keypair()
    _, peer_public = keypair()
    bridge = PersistentFleetBridge(
        FleetConfig(
            "bacloud",
            private,
            (FleetPeer("firstbyte", "https://firstbyte.example", peer_public, "peer-token"),),
            1.0,
            1.0,
        ),
        store,
        repo,
        object(),
        TaskStore(repo.path),
    )
    return SimpleNamespace(
        mesh=mesh, store=store, bridge=bridge, identity=identity, public_key=public
    )


def request_for(identity):
    return {
        "logical_agent_id": identity["logical_agent_id"],
        "work_session_id": identity["work_session_id"],
        "session_epoch": identity["session_epoch"],
        "requesting_instance_id": "firstbyte",
        "scope": "run",
        "principal_id": "client-1",
    }


@pytest.mark.asyncio
async def test_issue_permit_binds_node_and_session_deadline(native_authority):
    case = native_authority

    permit = await case.bridge.issue_permit(**request_for(case.identity))

    assert permit.node_instance_id == "firstbyte"
    assert permit.authority_node_id == "bacloud"
    assert verify_permit(permit, case.public_key)
    assert parse_utc(permit.permit_expires_at) <= parse_utc(permit.hard_expires_at)
    attachments = await case.store.attachments_for_session(
        permit.logical_agent_id, permit.work_session_id, permit.session_epoch
    )
    assert [item["node_instance_id"] for item in attachments] == ["firstbyte"]


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
async def test_remote_rejects_tampered_peer_permit(tmp_path):
    repo = SqliteRepository(tmp_path / "remote.sqlite3")
    await repo.initialize()
    authority_private, authority_public = keypair()
    remote_private, _ = keypair()
    now = utc_now()
    signed = sign_permit(
        PersistentCommandPermit(
            logical_agent_id="la_native",
            work_session_id="wm_native",
            session_epoch=1,
            authority_node_id="bacloud",
            authority_epoch=1,
            node_attachment_id="attachment",
            node_instance_id="firstbyte",
            scope="run",
            issued_at=utc_text(now),
            permit_expires_at=utc_text(now + timedelta(seconds=5)),
            hard_expires_at=utc_text(now + timedelta(minutes=2)),
            slot_revision=1,
            principal_id="client-1",
        ),
        authority_private,
    )
    # This field passes admission shape checks, so only signature verification can reject it.
    tampered = replace(signed, slot_revision=2)
    remote = PersistentFleetBridge(
        FleetConfig(
            "firstbyte",
            remote_private,
            (FleetPeer("bacloud", "https://bacloud.example", authority_public, "peer-token"),),
            1.0,
            1.0,
        ),
        PersistentAgentStore(repo.path),
        repo,
        object(),
        TaskStore(repo.path),
        client_factory=lambda: Client(tampered),
    )

    with pytest.raises(PersistentStoreError, match="authority_unavailable"):
        await remote.acquire_permit(
            logical_agent_id=signed.logical_agent_id,
            work_session_id=signed.work_session_id,
            session_epoch=signed.session_epoch,
            scope="run",
            principal_id=signed.principal_id,
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


@pytest.mark.asyncio
async def test_authority_rechecks_session_after_attachment_before_returning_permit(
    native_authority, monkeypatch
):
    case = native_authority
    original_record = case.store.record_node_attachment

    async def record_then_end(**kwargs):
        attachment = await original_record(**kwargs)
        case.mesh.apply_event(
            AccessSlotEvent(
                issuer_id="bacloud",
                slot_id="permit-slot",
                logical_agent_id="la_permit",
                revision=2,
                event_id="bacloud:permit-slot:2",
                kind="SessionEnded",
                effective_at=utc_now(),
            ),
            authenticated_peer_id="bacloud",
        )
        return attachment

    monkeypatch.setattr(case.store, "record_node_attachment", record_then_end)
    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await case.bridge.issue_permit(**request_for(case.identity))

    assert (
        await case.store.attachments_for_session(
            case.identity["logical_agent_id"],
            case.identity["work_session_id"],
            case.identity["session_epoch"],
            active_only=True,
        )
        == []
    )


@pytest.mark.asyncio
async def test_remote_drain_preserves_accepted_queued_command(
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
