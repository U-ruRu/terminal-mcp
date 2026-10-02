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
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore
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
async def test_persistent_attachment_presence_is_session_scoped_and_fenced(tmp_path):
    _, store, life, bridge, _, started, ctx, _ = await authority_fixture(tmp_path)
    logical_agent_id = started["logical_agent_id"]
    ws = started["work_session"]
    await bridge.materialize_session(
        logical_agent_id=logical_agent_id,
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
    )

    first = await bridge.update_attachment_presence(
        logical_agent_id=logical_agent_id,
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
        task_summary="Fleet work",
        intent="Implement authority",
        work_scope=["src/terminal_mcp"],
        details=["inspect", "implement", "verify"],
        current_step=2,
    )
    assert first["node_instance_id"] == "remote"
    assert first["work_session_id"] == ws["work_session_id"]
    assert first["session_epoch"] == ws["session_epoch"]
    assert first["current_step"] == 2

    visible = await store.attachment_presences_for_session(
        logical_agent_id, ws["work_session_id"], ws["session_epoch"]
    )
    assert [item["intent"] for item in visible] == ["Implement authority"]

    ended = await life.session_end(
        logical_agent_id,
        ws["work_session_id"],
        ws["session_epoch"],
        admission=ctx,
    )
    assert ended["work_session"]["state"] == "ended"
    assert (
        await store.attachment_presences_for_session(
            logical_agent_id, ws["work_session_id"], ws["session_epoch"]
        )
        == []
    )

    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await bridge.update_attachment_presence(
            logical_agent_id=logical_agent_id,
            work_session_id=ws["work_session_id"],
            session_epoch=ws["session_epoch"],
            requesting_instance_id="remote",
            task_summary="stale",
            intent="must not revive",
            work_scope=[],
            details=[],
            current_step=1,
        )


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


@pytest.mark.asyncio
async def test_control_store_v1_migrates_in_place_to_managed_v3_without_losing_routes(tmp_path):
    import sqlite3

    path = tmp_path / "control-v1.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE control_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL
            );
            INSERT INTO control_schema VALUES(1,1);
            CREATE TABLE control_meta(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                fleet_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                control_node_id TEXT NOT NULL,
                routing_revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            INSERT INTO control_meta VALUES(
                1,'fleet-a','node-a','node-a',7,'2026-01-01T00:00:00Z'
            );
            CREATE TABLE fleet_members(
                node_id TEXT PRIMARY KEY,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO fleet_members VALUES(
                'node-a','[]','active','2026-01-01T00:00:00Z'
            );
            CREATE TABLE authority_routes(
                logical_agent_id TEXT PRIMARY KEY,
                authority_node_id TEXT NOT NULL,
                authority_epoch INTEGER NOT NULL,
                routing_revision INTEGER NOT NULL,
                state TEXT NOT NULL,
                target_node_id TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO authority_routes VALUES(
                'logical-1','node-a',3,7,'active',NULL,'2026-01-01T00:00:00Z'
            );
            """
        )

    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )
    await store.initialize()

    assert await store.schema_version() == 3
    assert (await store.route("logical-1"))["routing_revision"] == 7
    state = await store.control_state()
    assert state["managed"] is False
    assert state["revisions"] == {
        "routing": 7,
        "topology": 0,
        "trust": 0,
        "access_policy": 0,
    }


@pytest.mark.asyncio
async def test_control_store_v2_migrates_single_mesh_membership_to_v3(tmp_path):
    import sqlite3

    legacy_private, legacy_public = keypair()
    path = tmp_path / "control-v2.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE control_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL
            );
            INSERT INTO control_schema VALUES(1,2);
            CREATE TABLE control_meta(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                fleet_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                control_node_id TEXT NOT NULL,
                routing_revision INTEGER NOT NULL DEFAULT 0,
                topology_revision INTEGER NOT NULL DEFAULT 0,
                trust_revision INTEGER NOT NULL DEFAULT 0,
                access_policy_revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            INSERT INTO control_meta VALUES(
                1,'fleet-a','node-a','node-a',4,7,5,3,'2026-01-01T00:00:00Z'
            );
            CREATE TABLE managed_mesh(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                mesh_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                adopted INTEGER NOT NULL,
                adopted_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO managed_mesh VALUES(
                1,'mesh-old','Legacy Mesh',1,
                '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z'
            );
            CREATE TABLE managed_nodes(
                node_id TEXT PRIMARY KEY,
                origin TEXT,
                public_key TEXT,
                auth_token TEXT,
                state TEXT NOT NULL,
                desired_topology_revision INTEGER NOT NULL,
                applied_topology_revision INTEGER NOT NULL,
                desired_trust_revision INTEGER NOT NULL,
                applied_trust_revision INTEGER NOT NULL,
                desired_policy_revision INTEGER NOT NULL,
                applied_policy_revision INTEGER NOT NULL,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO managed_nodes VALUES(
                'node-a',NULL,NULL,NULL,'active',7,7,5,5,3,3,NULL,'2026-01-02T00:00:00Z'
            );
            INSERT INTO managed_nodes VALUES(
                'node-b','https://node-b.example','pub','token','active',
                7,7,5,5,3,3,NULL,'2026-01-02T00:00:00Z'
            );
            INSERT INTO managed_nodes VALUES(
                'node-c','https://node-c.example','pub-c','token-c','detached',
                7,6,5,4,3,2,NULL,'2026-01-02T00:00:00Z'
            );
            CREATE TABLE managed_identity(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                active_private_key TEXT NOT NULL,
                active_generation INTEGER NOT NULL,
                pending_private_key TEXT,
                pending_generation INTEGER,
                updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO managed_identity("
            "singleton,active_private_key,active_generation,pending_private_key,"
            "pending_generation,updated_at) VALUES(1,?,1,NULL,NULL,?)",
            (legacy_private, "2026-01-02T00:00:00Z"),
        )

    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )
    await store.initialize()

    assert await store.schema_version() == 3
    state = await store.control_state()
    assert state["mesh"]["mesh_id"] == "mesh-old"
    assert [mesh["mesh_id"] for mesh in state["meshes"]] == ["mesh-old"]
    memberships = {node["node_id"]: node["mesh_id"] for node in state["nodes"]}
    assert memberships["node-a"] == "mesh-old"
    assert memberships["node-b"] == "mesh-old"
    assert memberships["node-c"] is None
    assert state["revisions"]["routing"] == 4
    assert state["revisions"]["topology"] == 7
    assert state["revisions"]["trust"] == 5
    identity = await store.ensure_managed_identity(legacy_private)
    assert identity["private_key"] == legacy_private
    assert identity["public_key"] == legacy_public
    assert identity["ingress_token"]
    restarted_identity = await store.ensure_managed_identity(legacy_private)
    assert restarted_identity["ingress_token"] == identity["ingress_token"]


@pytest.mark.asyncio
async def test_control_store_future_schema_fails_closed_before_creating_v2_tables(tmp_path):
    import sqlite3

    path = tmp_path / "control-future.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE control_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL
            );
            INSERT INTO control_schema VALUES(1,999);
            """
        )

    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )
    with pytest.raises(FleetControlError, match="unsupported fleet_control schema"):
        await store.initialize()

    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "managed_mesh" not in tables
    assert "access_policy" not in tables


@pytest.mark.asyncio
async def test_managed_mesh_topology_trust_and_access_policy_revisions_are_independent(tmp_path):
    store = FleetControlStore(
        tmp_path / "managed.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )
    await store.initialize()
    adopted = await store.adopt_managed(
        mesh_id="fleet-a",
        display_name="Production",
        nodes=[
            {"node_id": "node-a"},
            {
                "node_id": "node-b",
                "origin": "https://node-b.example",
                "public_key": "public-b",
                "auth_token": "token-b",
            },
        ],
        policy={
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": False,
        },
    )
    assert adopted["revisions"] == {
        "routing": 0,
        "topology": 1,
        "trust": 1,
        "access_policy": 1,
    }

    renamed = await store.rename_managed_mesh(
        "fleet-a",
        "Primary Mesh",
        expected_topology_revision=1,
    )
    assert renamed["revisions"]["topology"] == 2
    assert renamed["revisions"]["trust"] == 1
    assert renamed["revisions"]["access_policy"] == 1

    policy = await store.update_access_policy(
        duration_seconds=1500,
        warning_after_seconds=1200,
        alert_after_seconds=1400,
        rearm_after_seconds=180,
        legacy_admission_enabled=False,
        expected_revision=1,
    )
    assert policy["revision"] == 2
    state = await store.control_state()
    assert state["revisions"]["topology"] == 2
    assert state["revisions"]["trust"] == 1
    assert state["revisions"]["access_policy"] == 2
    node_b = next(node for node in state["nodes"] if node["node_id"] == "node-b")
    assert node_b["desired_topology_revision"] == 2
    assert node_b["desired_policy_revision"] == 2

    attached = await store.upsert_managed_node(
        node_id="node-b",
        mesh_id="fleet-a",
        origin="https://node-b.example",
        public_key="public-b",
        auth_token="token-b",
        expected_topology_revision=2,
    )
    assert attached["revisions"]["topology"] == 3
    assert attached["revisions"]["trust"] == 2

    changed = await store.upsert_managed_node(
        node_id="node-b",
        mesh_id="fleet-a",
        origin="https://node-b.example",
        public_key="public-b-2",
        auth_token="token-b-2",
        expected_topology_revision=3,
    )
    assert changed["revisions"]["topology"] == 3
    assert changed["revisions"]["trust"] == 3

    detached = await store.detach_managed_node("node-b", expected_topology_revision=3)
    assert detached["revisions"]["topology"] == 4
    node_b = next(node for node in detached["nodes"] if node["node_id"] == "node-b")
    assert node_b["state"] == "active"
    assert node_b["mesh_id"] is None


class _PolicyStub:
    def __init__(self):
        self.applied = []

    def snapshot(self):
        return {
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": False,
        }

    async def apply_managed(self, **policy):
        self.applied.append(policy)


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers, json):
        return await self.handler(url, headers, json)


@pytest.mark.asyncio
async def test_managed_control_mutation_forwards_to_control_node_and_applies_snapshot(tmp_path):
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    home_private, home_public = keypair()
    remote_private, remote_public = keypair()
    home_config = FleetConfig(
        "home",
        home_private,
        (FleetPeer("remote", "https://remote.example", remote_public, "remote-token"),),
        1.0,
        1.0,
    )
    remote_config = FleetConfig(
        "remote",
        remote_private,
        (FleetPeer("home", "https://home.example", home_public, "home-token"),),
        1.0,
        1.0,
    )
    home_store = FleetControlStore(
        tmp_path / "home-control.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    remote_store = FleetControlStore(
        tmp_path / "remote-control.sqlite3",
        fleet_id="fleet-a",
        node_id="remote",
        control_node_id="home",
    )
    await home_store.initialize()
    await remote_store.initialize()

    replicated_state = None

    async def home_http(url, headers, body):
        nonlocal replicated_state
        assert url.endswith("/internal/fleet/control/apply")
        replicated_state = body["state"]
        return _FakeResponse({"ok": True, "control": body["state"]})

    home = ManagedFleetControl(
        home_store,
        home_config,
        _PolicyStub(),
        public_base_url="https://home.example",
        client_factory=lambda: _FakeClient(home_http),
    )
    created = await home.adopt(mesh_id="mesh-a", display_name="Before")
    assert [node["node_id"] for node in created["nodes"]] == ["home"]
    await home.upsert_node(
        node_id="remote", mesh_id="mesh-a", origin="https://remote.example",
        public_key=remote_public, auth_token="remote-token",
        expected_topology_revision=created["revisions"]["topology"],
    )

    async def remote_http(url, headers, body):
        assert headers["X-Terminal-MCP-Peer"] == "remote"
        assert url.endswith("/internal/fleet/control/mutate/rename")
        control = await home.execute_forwarded("rename", body["payload"])
        return _FakeResponse({"ok": True, "control": control})

    remote_policy = _PolicyStub()
    remote = ManagedFleetControl(
        remote_store,
        remote_config,
        remote_policy,
        public_base_url="https://remote.example",
        client_factory=lambda: _FakeClient(remote_http),
    )
    initial = await home.snapshot()
    assert replicated_state is not None
    assert any(
        item.get("node_id") == "remote" and item.get("auth_token") == "remote-token"
        for item in replicated_state.get("_peer_material", [])
    )
    await remote.apply_replica(replicated_state, source_node_id="home")
    assert remote.config.local_auth_token == "remote-token"

    renamed = await remote.rename(
        "After",
        expected_topology_revision=initial["revisions"]["topology"],
    )
    assert renamed["mesh"]["mesh_id"] == "mesh-a"
    assert renamed["mesh"]["display_name"] == "After"
    assert renamed["meshes"][0]["display_name"] == "After"
    assert renamed["revisions"]["topology"] == initial["revisions"]["topology"] + 1
    assert (await home.snapshot())["meshes"][0]["display_name"] == "After"
    assert (await remote.snapshot())["meshes"][0]["display_name"] == "After"
    assert remote_policy.applied


@pytest.mark.asyncio
async def test_non_bootstrap_node_enrolls_and_forwards_through_durable_managed_trust(tmp_path):
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    home_private, _ = keypair()
    remote_private, _ = keypair()
    home_config = FleetConfig("home", home_private, (), 1.0, 1.0)
    remote_config = FleetConfig("remote", remote_private, (), 1.0, 1.0)
    home_store = FleetControlStore(
        tmp_path / "non-bootstrap-home.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    remote_store = FleetControlStore(
        tmp_path / "non-bootstrap-remote.sqlite3",
        fleet_id="fleet-a",
        node_id="remote",
        control_node_id="home",
    )
    await home_store.initialize()
    await remote_store.initialize()

    home = None
    remote = None
    home_enrollment = None
    remote_enrollment = None

    async def home_http(url, headers, body):
        assert remote is not None
        assert remote_enrollment is not None
        assert url == "https://remote.example/internal/fleet/control/apply"
        assert headers["X-Terminal-MCP-Peer"] == "home"
        assert headers["Authorization"] == "Bearer " + remote_enrollment["auth_token"]
        authenticated = await remote.authenticate_management_peer(
            "home",
            headers["Authorization"],
            first_apply_control_node_id=body["state"]["control_node_id"],
        )
        assert authenticated == "home"
        control = await remote.apply_replica(body["state"], source_node_id="home")
        return _FakeResponse({"ok": True, "control": control})

    async def remote_http(url, headers, body):
        assert home is not None
        assert home_enrollment is not None
        assert url == "https://home.example/internal/fleet/control/mutate/rename"
        assert headers["X-Terminal-MCP-Peer"] == "remote"
        assert headers["Authorization"] == "Bearer " + home_enrollment["auth_token"]
        authenticated = await home.authenticate_management_peer(
            "remote",
            headers["Authorization"],
        )
        assert authenticated == "remote"
        control = await home.execute_forwarded(
            "rename",
            body["payload"],
            authenticated_peer_id="remote",
        )
        return _FakeResponse({"ok": True, "control": control})

    home = ManagedFleetControl(
        home_store,
        home_config,
        _PolicyStub(),
        public_base_url="https://home.example",
        client_factory=lambda: _FakeClient(home_http),
    )
    remote = ManagedFleetControl(
        remote_store,
        remote_config,
        _PolicyStub(),
        public_base_url="https://remote.example",
        client_factory=lambda: _FakeClient(remote_http),
    )

    home_enrollment = await home.enrollment_descriptor()
    remote_enrollment = await remote.enrollment_descriptor()
    assert home_config.peers == ()
    assert remote_config.peers == ()
    assert "private_key" not in home_enrollment
    assert "private_key" not in remote_enrollment

    adopted = await home.adopt(mesh_id="mesh-a", display_name="Before")
    home_attached = await home.upsert_node(
        node_id="home",
        mesh_id="mesh-a",
        origin=None,
        public_key=None,
        auth_token=None,
        expected_topology_revision=adopted["revisions"]["topology"],
    )
    attached = await home.upsert_node(
        node_id=remote_enrollment["node_id"],
        mesh_id="mesh-a",
        origin=remote_enrollment["origin"],
        public_key=remote_enrollment["public_key"],
        auth_token=remote_enrollment["auth_token"],
        expected_topology_revision=home_attached["revisions"]["topology"],
    )
    remote_state = await remote.snapshot()
    remote_node = next(node for node in remote_state["nodes"] if node["node_id"] == "remote")
    assert remote_node["mesh_id"] == "mesh-a"
    assert "_peer_material" not in remote_state
    assert remote_state["nodes"][0].get("auth_token") is None

    remote_home_material = await remote_store.managed_node("home")
    assert remote_home_material is not None
    assert remote_home_material["auth_token"] == home_enrollment["auth_token"]
    assert home.config.local_auth_token == home_enrollment["auth_token"]
    assert remote.config.local_auth_token == remote_enrollment["auth_token"]
    home_remote = home.config.peers_by_id["remote"]
    remote_home = remote.config.peers_by_id["home"]
    assert home_remote.auth_token == remote_enrollment["auth_token"]
    assert remote_home.auth_token == home_enrollment["auth_token"]
    assert home.config.outbound_auth_token(home_remote) == home_enrollment["auth_token"]
    assert remote.config.outbound_auth_token(remote_home) == remote_enrollment["auth_token"]

    renamed = await remote.rename(
        "After",
        expected_topology_revision=attached["revisions"]["topology"],
    )
    assert renamed["meshes"][0]["display_name"] == "After"
    assert (await home.snapshot())["meshes"][0]["display_name"] == "After"


@pytest.mark.asyncio
async def test_managed_control_retries_only_pending_member_until_converged(tmp_path):
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    home_private, _ = keypair()
    _, remote_public = keypair()
    config = FleetConfig(
        "home",
        home_private,
        (FleetPeer("remote", "https://remote.example", remote_public, "remote-token"),),
        1.0,
        1.0,
    )
    store = FleetControlStore(
        tmp_path / "retry-control.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    await store.initialize()
    attempts = 0
    fail = True

    async def handler(url, headers, body):
        nonlocal attempts
        attempts += 1
        if fail:
            raise RuntimeError("offline")
        return _FakeResponse({"ok": True, "control": body["state"]})

    control = ManagedFleetControl(
        store,
        config,
        _PolicyStub(),
        public_base_url="https://home.example",
        client_factory=lambda: _FakeClient(handler),
    )
    created = await control.adopt(mesh_id="mesh-a", display_name="Fleet")
    assert [node["node_id"] for node in created["nodes"]] == ["home"]
    await control.upsert_node(
        node_id="remote", mesh_id="mesh-a", origin="https://remote.example",
        public_key=remote_public, auth_token="remote-token",
        expected_topology_revision=created["revisions"]["topology"],
    )
    state = await control.snapshot()
    remote = next(node for node in state["nodes"] if node["node_id"] == "remote")
    assert remote["last_error"] == "reconcile_failed:RuntimeError"
    first_attempts = attempts

    fail = False
    converged = await control.reconcile_pending()
    assert attempts == first_attempts + 1
    remote = next(node for node in converged["nodes"] if node["node_id"] == "remote")
    assert remote["last_error"] is None
    assert remote["desired_topology_revision"] == remote["applied_topology_revision"]
    assert remote["desired_trust_revision"] == remote["applied_trust_revision"]
    assert remote["desired_policy_revision"] == remote["applied_policy_revision"]

    await control.reconcile_pending()
    assert attempts == first_attempts + 1


@pytest.mark.asyncio
async def test_managed_trust_rotation_keeps_private_key_local_and_survives_restart(tmp_path):
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    home_private, _ = keypair()
    _, remote_public = keypair()
    bootstrap = FleetConfig(
        "home",
        home_private,
        (FleetPeer("remote", "https://remote.example", remote_public, "remote-token"),),
        1.0,
        1.0,
    )
    store = FleetControlStore(
        tmp_path / "trust-rotation.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    await store.initialize()

    async def handler(url, headers, body):
        assert url.endswith("/internal/fleet/control/apply")
        return _FakeResponse({"ok": True, "control": body["state"]})

    control = ManagedFleetControl(
        store,
        bootstrap,
        _PolicyStub(),
        public_base_url="https://home.example",
        client_factory=lambda: _FakeClient(handler),
    )
    adopted = await control.adopt(mesh_id="mesh-a", display_name="Fleet")
    before_revision = adopted["revisions"]["trust"]
    before_private = control.config.signing_private_key

    rotated = await control.rotate_local_trust(expected_trust_revision=before_revision)
    assert rotated["revisions"]["trust"] == before_revision + 1
    assert control.config.signing_private_key != before_private
    local_node = next(node for node in rotated["nodes"] if node["node_id"] == "home")
    identity = await store.managed_identity()
    assert identity is not None
    assert identity["public_key"] == local_node["public_key"]
    assert identity["generation"] == 2
    assert "private_key" not in rotated
    assert "peer_material" not in rotated

    restarted = ManagedFleetControl(
        store,
        bootstrap,
        _PolicyStub(),
        public_base_url="https://home.example",
        client_factory=lambda: _FakeClient(handler),
    )
    await restarted.reconcile_local()
    assert restarted.config.signing_private_key == identity["private_key"]
    assert restarted.config.signing_private_key != bootstrap.signing_private_key


@pytest.mark.asyncio
async def test_forwarded_trust_rotation_can_only_publish_authenticated_peer_key(tmp_path):
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    home_private, _ = keypair()
    _, remote_public = keypair()
    config = FleetConfig(
        "home",
        home_private,
        (FleetPeer("remote", "https://remote.example", remote_public, "remote-token"),),
        1.0,
        1.0,
    )
    store = FleetControlStore(
        tmp_path / "trust-forward.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    await store.initialize()
    control = ManagedFleetControl(
        store, config, _PolicyStub(), public_base_url="https://home.example"
    )
    await control.adopt(mesh_id="mesh-a", display_name="Fleet")
    state = await control.snapshot()

    with pytest.raises(FleetControlError, match="trust_rotation_peer_mismatch"):
        await control.execute_forwarded(
            "rotate-trust",
            {
                "node_id": "home",
                "public_key": remote_public,
                "expected_trust_revision": state["revisions"]["trust"],
            },
            authenticated_peer_id="remote",
        )


@pytest.mark.asyncio
async def test_detach_is_delivered_before_revocation_and_does_not_restore_stale_bootstrap_peers(
    tmp_path,
):
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    home_private, home_public = keypair()
    remote_private, remote_public = keypair()
    home_config = FleetConfig(
        "home",
        home_private,
        (FleetPeer("remote", "https://remote.example", remote_public, "pair-token"),),
        1.0,
        1.0,
    )
    remote_config = FleetConfig(
        "remote",
        remote_private,
        (FleetPeer("home", "https://home.example", home_public, "pair-token"),),
        1.0,
        1.0,
    )
    home_store = FleetControlStore(
        tmp_path / "detach-home.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    remote_store = FleetControlStore(
        tmp_path / "detach-remote.sqlite3",
        fleet_id="fleet-a",
        node_id="remote",
        control_node_id="home",
    )
    await home_store.initialize()
    await remote_store.initialize()
    remote = ManagedFleetControl(
        remote_store,
        remote_config,
        _PolicyStub(),
        public_base_url="https://remote.example",
    )

    async def home_http(url, headers, body):
        assert url.endswith("/internal/fleet/control/apply")
        applied = await remote.apply_replica(body["state"], source_node_id="home")
        return _FakeResponse({"ok": True, "control": applied})

    home = ManagedFleetControl(
        home_store,
        home_config,
        _PolicyStub(),
        public_base_url="https://home.example",
        client_factory=lambda: _FakeClient(home_http),
    )
    adopted = await home.adopt(mesh_id="mesh-a", display_name="Fleet")
    assert adopted["managed"] is True
    assert [node["node_id"] for node in adopted["nodes"]] == ["home"]
    assert (await remote.snapshot())["managed"] is False
    attached = await home.upsert_node(
        node_id="remote",
        mesh_id="mesh-a",
        origin="https://remote.example",
        public_key=remote_public,
        auth_token="pair-token",
        expected_topology_revision=adopted["revisions"]["topology"],
    )
    assert (await remote.snapshot())["mesh"]["mesh_id"] == "mesh-a"

    detached = await home.detach_node(
        "remote",
        expected_topology_revision=attached["revisions"]["topology"],
    )
    remote_state = await remote.snapshot()
    assert detached["managed"] is True
    remote_member = next(node for node in detached["nodes"] if node["node_id"] == "remote")
    assert remote_member["state"] == "active"
    assert remote_member["mesh_id"] is None
    assert remote_state["managed"] is True
    assert remote_state["mesh"] is None
    assert home.config.peers == ()
    assert remote.config.peers == ()


@pytest.mark.asyncio
async def test_multi_mesh_membership_supports_standalone_attach_move_and_detach(tmp_path):
    store = FleetControlStore(
        tmp_path / "multi-mesh.sqlite3",
        fleet_id="fleet-a",
        node_id="control",
        control_node_id="control",
    )
    await store.initialize()
    policy = {
        "duration_seconds": 1380,
        "warning_after_seconds": 1200,
        "alert_after_seconds": 1320,
        "rearm_after_seconds": 180,
        "legacy_admission_enabled": False,
    }
    state = await store.adopt_managed(
        mesh_id="mesh-a",
        display_name="Alpha",
        nodes=[
            {"node_id": "control"},
            {
                "node_id": "node-b",
                "origin": "https://node-b.example",
                "public_key": "public-b",
                "auth_token": "token-b",
            },
        ],
        policy=policy,
    )
    state = await store.adopt_managed(
        mesh_id="mesh-b",
        display_name="Bravo",
        nodes=[
            {"node_id": "control"},
            {
                "node_id": "node-b",
                "origin": "https://node-b.example",
                "public_key": "public-b",
                "auth_token": "token-b",
            },
        ],
        policy=policy,
    )
    assert {mesh["mesh_id"] for mesh in state["meshes"]} == {"mesh-a", "mesh-b"}
    assert state["mesh"] is None
    assert all(node["mesh_id"] is None for node in state["nodes"])

    state = await store.upsert_managed_node(
        node_id="node-b",
        mesh_id="mesh-a",
        origin=None,
        public_key=None,
        auth_token=None,
        expected_topology_revision=state["revisions"]["topology"],
    )
    node_b = next(node for node in state["nodes"] if node["node_id"] == "node-b")
    assert node_b["mesh_id"] == "mesh-a"

    moved = await store.move_managed_node(
        "node-b",
        "mesh-b",
        expected_topology_revision=state["revisions"]["topology"],
    )
    node_b = next(node for node in moved["nodes"] if node["node_id"] == "node-b")
    assert node_b["mesh_id"] == "mesh-b"
    assert sum(node["node_id"] == "node-b" for node in moved["nodes"]) == 1

    detached = await store.detach_managed_node(
        "node-b",
        expected_topology_revision=moved["revisions"]["topology"],
    )
    node_b = next(node for node in detached["nodes"] if node["node_id"] == "node-b")
    assert node_b["state"] == "active"
    assert node_b["mesh_id"] is None

@pytest.mark.asyncio
async def test_control_authority_rehome_is_persisted_and_requires_standalone(tmp_path):
    path = tmp_path / "rehome-control.sqlite3"
    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="remote",
        control_node_id="main",
    )
    await store.initialize()
    assert store.control_node_id == "main"

    claimed = await store.claim_local_control_authority()
    assert claimed == "remote"
    assert store.control_node_id == "remote"

    restarted = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="remote",
        control_node_id="main",
    )
    await restarted.initialize()
    assert restarted.control_node_id == "remote"
    assert (await restarted.control_state())["control_node_id"] == "remote"

    await restarted.adopt_managed(
        mesh_id="mesh-a",
        display_name="Remote Mesh",
        nodes=[{"node_id": "remote"}],
        policy={
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": False,
        },
    )
    await restarted.upsert_managed_node(
        node_id="remote",
        mesh_id="mesh-a",
        origin=None,
        public_key=None,
        auth_token=None,
        expected_topology_revision=1,
    )
    with pytest.raises(FleetControlError, match="control_authority_rehome_requires_standalone"):
        await restarted.claim_local_control_authority()

@pytest.mark.asyncio
async def test_message_permit_is_fenced_by_the_same_persistent_session(tmp_path):
    _, store, life, bridge, _, started, ctx, _ = await authority_fixture(tmp_path)
    logical_agent_id = started["logical_agent_id"]
    ws = started["work_session"]

    permit = await bridge.issue_permit(
        logical_agent_id=logical_agent_id,
        work_session_id=ws["work_session_id"],
        session_epoch=ws["session_epoch"],
        requesting_instance_id="remote",
        scope="message",
        operation="message",
        request_id="message-request-1",
        principal_id=ctx.principal_id,
    )
    assert permit.logical_agent_id == logical_agent_id
    assert permit.work_session_id == ws["work_session_id"]
    assert permit.session_epoch == ws["session_epoch"]
    assert permit.scope == "message"

    await life.session_end(
        logical_agent_id,
        ws["work_session_id"],
        ws["session_epoch"],
        admission=ctx,
    )

    with pytest.raises(PersistentStoreError, match="session_not_active"):
        await bridge.issue_permit(
            logical_agent_id=logical_agent_id,
            work_session_id=ws["work_session_id"],
            session_epoch=ws["session_epoch"],
            requesting_instance_id="remote",
            scope="message",
            operation="message",
            request_id="message-request-ended",
            principal_id=ctx.principal_id,
        )

    active = await store.active_session_for_slot(logical_agent_id)
    assert active is None


@pytest.mark.asyncio
async def test_fleet_bridge_starts_with_empty_remote_obligation_cache(tmp_path):
    _, _, _, bridge, _, started, _, _ = await authority_fixture(tmp_path)
    assert bridge.cached_obligations(started["logical_agent_id"]) == []
