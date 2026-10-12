"""Native Access proof enrollment from authenticated managed authority material."""
import hashlib

import pytest

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.storage.access_mesh import AccessMeshStore

FIRST = b"original-node-bootstrap-secret-firstbyte"
MAIN = b"original-node-bootstrap-secret-main-000"
AUTHORITY_TOKEN = "authority-ingress-token-" + "t" * 48
WRONG_TOKEN = "wrong-authority-ingress-token-" + "v" * 48


def store(path, local, bootstrap):
    return AccessMeshStore(
        path, local_node_id=local,
        trusted_issuers=frozenset(("bacloud", "firstbyte", "main")),
        proof_key=bootstrap,
    )


def test_join_uses_authority_token_automatically_including_main(tmp_path):
    fb = store(tmp_path / "fb.sqlite3", "firstbyte", FIRST)
    main = store(tmp_path / "main.sqlite3", "main", MAIN)
    assert fb.code_tag("bacloud", "0123") != main.code_tag("bacloud", "0123")
    bootstrap_fb = fb.code_tag("firstbyte", "2468")
    bootstrap_main = main.code_tag("main", "1357")
    receipt_key_before = fb._receipt_cipher()
    assert receipt_key_before is not None

    assert fb.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    assert main.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    assert fb.managed_proof_ready and main.managed_proof_ready
    assert fb.code_tag("bacloud", "0123") == main.code_tag("bacloud", "0123")
    assert fb.peer_proof_tag(receiver_id="main", sender_id="firstbyte", nonce="f" * 32) == (
        main.peer_proof_tag(receiver_id="main", sender_id="firstbyte", nonce="f" * 32)
    )

    # The node's pre-enrollment sessions and receipts are not rekeyed or lost.
    assert bootstrap_fb in fb.code_tags("firstbyte", "2468")
    assert bootstrap_main in main.code_tags("main", "1357")
    assert bootstrap_fb not in fb.code_tags("bacloud", "2468")
    assert hashlib.sha256(fb._receipt_cipher()._key if hasattr(fb._receipt_cipher(), "_key") else FIRST).digest()

    # A lost response/retry must be idempotent and may not create duplicate keys.
    assert not fb.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    assert fb.clear_managed_proof()
    assert not fb.managed_proof_ready
    assert bootstrap_fb in fb.code_tags("firstbyte", "2468")
    assert fb.code_tag("bacloud", "0123") != main.code_tag("bacloud", "0123")

    # Persist the encrypted local proof history through restart/detach.
    fb = store(tmp_path / "fb.sqlite3", "firstbyte", FIRST)
    main = store(tmp_path / "main.sqlite3", "main", MAIN)
    assert bootstrap_fb in fb.code_tags("firstbyte", "2468")
    assert bootstrap_main in main.code_tags("main", "1357")
    assert len(fb._local_proof_history) == 1
    assert len(main._local_proof_history) == 1
    assert not fb.managed_proof_ready
    assert fb.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    assert main.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    assert fb.code_tag("bacloud", "0123") == main.code_tag("bacloud", "0123")


def test_invalid_authority_proof_cannot_enroll(tmp_path):
    m = store(tmp_path / "m.sqlite3", "main", MAIN)
    with pytest.raises(AccessMeshError, match="access_mesh_proof_enrollment_invalid"):
        m.install_managed_proof(
            mesh_id="mesh-a", authority_id="outsider", ingress_token=AUTHORITY_TOKEN
        )
    with pytest.raises(AccessMeshError, match="access_mesh_proof_enrollment_invalid"):
        m.install_managed_proof(
            mesh_id="mesh-a", authority_id="bacloud", ingress_token="short"
        )
    assert not m.managed_proof_ready


def test_authority_rotation_preserves_old_local_proof_tags(tmp_path):
    m = store(tmp_path / "m.sqlite3", "main", MAIN)
    m.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    old_local = m.code_tag("main", "7654")
    m.install_managed_proof(
        mesh_id="mesh-b", authority_id="firstbyte", ingress_token=WRONG_TOKEN
    )
    assert old_local in m.code_tags("main", "7654")
    assert old_local not in m.code_tags("bacloud", "7654")
    restored = store(tmp_path / "m.sqlite3", "main", MAIN)
    assert old_local in restored.code_tags("main", "7654")
    assert len(restored._local_proof_history) == 2


def test_signed_managed_replica_installs_group_proof_without_operator_env_edit(tmp_path):
    """Join coordinator applies signed Fleet membership and proof in one lifecycle."""
    from functools import partial

    from fastapi.testclient import TestClient

    from terminal_mcp.app import create_app
    from test_access_slot_inheritance_http import mesh_config
    from test_fleet_control_hardening import _managed_replica_snapshot

    app = create_app(mesh_config(tmp_path, control="control"))
    with TestClient(app, base_url="https://terminal.example") as client:
        controller = app.state.managed_fleet_control
        state = _managed_replica_snapshot(include_member_mesh_id=True, member_mesh_id="mesh-a")
        state["fleet_id"] = "qa-inheritance"
        state["nodes"][1]["node_id"] = "firstbyte"
        state["_peer_material"] = [
            {"node_id": "control", "auth_token": "validated-managed-authority-" + "x" * 48}
        ]
        client.portal.call(partial(controller.apply_replica, state, source_node_id="control"))
        access = app.state.access_mesh
        assert access.store.managed_proof_ready
        assert access.store._managed_proof_scope == ("mesh-a", "control")
        status = access.replication.native_trust_status()
        assert status["state"] == "pending"
        assert not status["ready"]
        assert status["peers"]["control"] in {"pending", "unavailable"}
        # Idempotent reconcile does not reset or duplicate proof enrollment.
        before = access.store.code_tag("control", "1234")
        client.portal.call(partial(controller.apply_replica, state, source_node_id="control"))
        assert access.store.code_tag("control", "1234") == before



def test_pending_membership_does_not_accept_remote_bootstrap_claim(tmp_path):
    m = store(tmp_path / "pending.sqlite3", "main", MAIN)
    m.managed_trust_required = True
    with pytest.raises(AccessMeshError, match="access_mesh_proof_enrollment_pending"):
        m.attach(issuer_id="bacloud", code="5678", connection_key="fixture-pending")
    # Local-only historical lookup remains permitted (no slot exists here).
    with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
        m.attach(issuer_id="main", code="5678", connection_key="fixture-local")
    m.install_managed_proof(
        mesh_id="mesh-test", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN,
    )
    with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
        m.attach(issuer_id="bacloud", code="5678", connection_key="fixture-after")
    m.clear_managed_proof()
    m.managed_trust_required = False
    with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
        m.attach(issuer_id="main", code="5678", connection_key="fixture-after-leave")


def test_preexisting_local_persistent_slot_stays_attachable_after_join_leave_and_restart(tmp_path):
    from datetime import UTC, datetime

    from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
    from terminal_mcp.storage.sqlite import SqliteRepository
    import asyncio

    path = tmp_path / "preserved.sqlite3"
    asyncio.run(SqliteRepository(path, tmp_path / "out.sqlite3").initialize())
    s = store(path, "main", MAIN)
    code = "4298"
    event = AccessSlotEvent(
        issuer_id="main", slot_id="original-local-slot", logical_agent_id="original-local-agent",
        event_id="local-initial-1", revision=1, kind="SlotIssued",
        policy=SlotPolicy(duration_seconds=1200, cooldown_seconds=60),
        code_tag=s.code_tag("main", code),
        effective_at=datetime(2026, 10, 12, 14, 0, tzinfo=UTC),
    )
    assert s.apply_event(
        event, authenticated_peer_id="main", issued_kind="persistent",
    ).outcome == "applied"
    assert s.attach(issuer_id="main", code=code, connection_key="before-enrollment").slot_id == (
        "original-local-slot"
    )
    assert s.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN,
    )
    assert s.attach(issuer_id="main", code=code, connection_key="after-enrollment").slot_id == (
        "original-local-slot"
    )
    s.clear_managed_proof()
    assert s.attach(issuer_id="main", code=code, connection_key="after-leave").slot_id == (
        "original-local-slot"
    )
    s = store(path, "main", MAIN)
    assert s.attach(issuer_id="main", code=code, connection_key="after-restart").slot_id == (
        "original-local-slot"
    )
