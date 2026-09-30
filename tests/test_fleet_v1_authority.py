import base64
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.core.persistent_fleet import (
    PersistentCommandPermit,
    PersistentFleetBridge,
    sign_permit,
)
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.control_storage import FleetControlStore
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
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
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


async def authority_fixture(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    life = PersistentLifecycleCoordinator(
        store,
        enabled=True,
        authority_node_id="home",
        session_duration_seconds=120,
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
        origin_instance_id="home",
    )
    home_private, home_public = keypair()
    _, remote_public = keypair()
    config = FleetConfig(
        "home",
        home_private,
        (FleetPeer("remote", "https://remote.example", remote_public, "remote-token"),),
        1.0,
        1.0,
    )
    control = FleetControlStore(
        tmp_path / "fleet-control.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    await control.initialize()
    await control.publish_route(
        started["logical_agent_id"],
        "home",
        started["work_session"]["authority_epoch"],
    )
    bridge = PersistentFleetBridge(
        config,
        store,
        repo,
        object(),
        TaskStore(repo.path),
        control_store=control,
        control_node_id="home",
    )
    return repo, store, life, bridge, control, started, ctx, home_public


@pytest.mark.asyncio
async def test_control_store_routes_and_transfer_are_monotonic(tmp_path):
    store = FleetControlStore(
        tmp_path / "control.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )
    await store.initialize()
    assert store.sqlite_diagnostics.role == "fleet_control"

    first = await store.publish_route("logical-1", "node-a", 3)
    assert first["routing_revision"] == 1
    duplicate = await store.publish_route("logical-1", "node-a", 3)
    assert duplicate["routing_revision"] == 1

    prepared = await store.prepare_transfer("logical-1", "node-a", "node-b", 3)
    assert prepared["phase"] == "prepare"
    assert prepared["target_authority_epoch"] == 4

    imported = await store.advance_transfer("logical-1", "imported")
    assert imported["routing_revision"] > prepared["routing_revision"]
    committed = await store.advance_transfer("logical-1", "committed")
    route = await store.route("logical-1")
    assert committed["phase"] == "committed"
    assert route["authority_node_id"] == "node-b"
    assert route["authority_epoch"] == 4
    active = await store.advance_transfer("logical-1", "active")
    assert active["phase"] == "active"
    assert (await store.route("logical-1"))["state"] == "active"


@pytest.mark.asyncio
async def test_materialize_is_idempotent_and_never_creates_second_session(tmp_path):
    _, store, _, bridge, _, started, _, _ = await authority_fixture(tmp_path)
    ws = started["work_session"]

    first = await bridge.materialize_session(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
    )
    second = await bridge.materialize_session(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
    )
    assert first["attachment"]["node_attachment_id"] == second["attachment"]["node_attachment_id"]
    active = await store.active_session_for_slot(started["logical_agent_id"])
    assert active.work_session_id == ws["work_session_id"]
    assert active.session_epoch == ws["session_epoch"]
    attachments = await store.attachments_for_session(
        started["logical_agent_id"], ws["work_session_id"], ws["session_epoch"]
    )
    assert len(attachments) == 1


@pytest.mark.asyncio
async def test_request_dedup_and_message_gate_share_one_home_obligation(tmp_path):
    _, store, _, bridge, _, started, ctx, _ = await authority_fixture(tmp_path)
    ws = started["work_session"]
    await bridge.materialize_session(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
    )

    first = await bridge.issue_permit(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
        scope="run",
        operation="run",
        request_id="request-1",
        principal_id=ctx.principal_id,
    )
    replay = await bridge.issue_permit(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
        scope="run",
        operation="run",
        request_id="request-1",
        principal_id=ctx.principal_id,
    )
    assert replay == first

    obligation = await store.create_message_obligation(
        message_ref="home:msg:1",
        logical_agent_id=started["logical_agent_id"],
        sender_agent_id="manager",
        text="reply required",
        require_reply=True,
        alert=False,
    )
    with pytest.raises(PersistentStoreError, match="coordination_blocked"):
        await bridge.issue_permit(
            logical_agent_id=started["logical_agent_id"],
            work_session_id=ws["work_session_id"],
            session_epoch=ws["session_epoch"],
            requesting_instance_id="remote",
            scope="run",
            operation="run",
            request_id="request-2",
            principal_id=ctx.principal_id,
        )

    resolved = await bridge.receive_obligation_receipt(
        message_ref=obligation["message_ref"],
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        attachment_node_id="remote",
        read_at=utc_text(),
        replied_at=utc_text(),
        reply_message_ref="remote:reply:1",
    )
    assert resolved["gate"]["blocked"] is False
    assert resolved["gate"]["gate_revision"] > obligation["gate_revision"]
    permit = await bridge.issue_permit(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
        scope="run",
        operation="run",
        request_id="request-3",
        principal_id=ctx.principal_id,
    )
    assert permit.gate_revision == resolved["gate"]["gate_revision"]


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class RetryClient:
    def __init__(self, old_origin, new_origin, permit):
        self.old_origin = old_origin
        self.new_origin = new_origin
        self.permit = permit
        self.request_ids = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, *, headers, json):
        self.request_ids.append(json["request_id"])
        if url.startswith(self.old_origin):
            return FakeResponse(
                409,
                {
                    "detail": {
                        "code": "wrong_authority",
                        "blockers": [
                            {
                                "authority_node_id": "new-home",
                                "authority_epoch": 2,
                                "routing_revision": 9,
                            }
                        ],
                    }
                },
            )
        assert url.startswith(self.new_origin)
        return FakeResponse(200, {"permit": self.permit.as_dict()})


@pytest.mark.asyncio
async def test_wrong_authority_retries_once_with_same_request_id(tmp_path):
    local_private, _ = keypair()
    _, old_public = keypair()
    new_private, new_public = keypair()
    config = FleetConfig(
        "remote",
        local_private,
        (
            FleetPeer("old-home", "https://old.example", old_public, "old-token"),
            FleetPeer("new-home", "https://new.example", new_public, "new-token"),
        ),
        1.0,
        1.0,
    )
    control = FleetControlStore(
        tmp_path / "control.sqlite3",
        fleet_id="fleet-a",
        node_id="remote",
        control_node_id="old-home",
    )
    await control.initialize()
    await control.publish_route("logical-1", "old-home", 1)

    now = utc_now()
    permit = sign_permit(
        PersistentCommandPermit(
            logical_agent_id="logical-1",
            work_session_id="ws-1",
            session_epoch=1,
            authority_node_id="new-home",
            authority_epoch=2,
            node_attachment_id="att-1",
            node_instance_id="remote",
            scope="run",
            issued_at=utc_text(now),
            permit_expires_at=utc_text(now + timedelta(seconds=5)),
            hard_expires_at=utc_text(now + timedelta(seconds=30)),
            slot_revision=4,
            principal_id="client-1",
            operation="run",
            gate_revision=3,
            ttl_ms=5000,
        ),
        new_private,
    )
    client = RetryClient("https://old.example", "https://new.example", permit)
    bridge = PersistentFleetBridge(
        config,
        object(),
        object(),
        object(),
        object(),
        client_factory=lambda: client,
        control_store=control,
        control_node_id="old-home",
    )
    accepted = await bridge.acquire_permit(
        logical_agent_id="logical-1",
        work_session_id="ws-1",
        session_epoch=1,
        scope="run",
        operation="run",
        request_id="same-request",
        principal_id="client-1",
    )
    assert accepted == permit
    assert client.request_ids == ["same-request", "same-request"]
    route = await control.route("logical-1")
    assert route["authority_node_id"] == "new-home"
    assert route["authority_epoch"] == 2


@pytest.mark.asyncio
async def test_monotonic_deadline_is_not_reusable_after_restart(tmp_path):
    _, store, _, bridge, _, started, ctx, _ = await authority_fixture(tmp_path)
    ws = started["work_session"]
    permit = await bridge.issue_permit(
        logical_agent_id=started["logical_agent_id"],
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
        scope="run",
        principal_id=ctx.principal_id,
    )
    remote_private, _ = keypair()
    remote = PersistentFleetBridge(
        FleetConfig("remote", remote_private, (), 1.0, 1.0),
        store,
        object(),
        object(),
        object(),
    )
    with pytest.raises(PersistentStoreError, match="permit_expired"):
        remote.ensure_permit_valid(replace(permit))
