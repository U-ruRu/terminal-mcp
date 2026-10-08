"""Contract tests for isolated event-driven access replicas and attach-only writes."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    AccessSlotEvent,
    LocalAccessMesh,
    SlotPolicy,
)

UTC = UTC
T0 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
KEY = b"unit-fixture-key-not-used-in-production-001"


@pytest.fixture
def replica(tmp_path):
    return LocalAccessMesh(
        tmp_path / "access-mesh.db",
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )


def issued(replica, issuer="bacloud", slot="slot-1", agent="agent-1",
           code="1234", kind="legacy", policy=None, start=T0):
    event = AccessSlotEvent(
        issuer_id=issuer, slot_id=slot, logical_agent_id=agent,
        event_id=f"{issuer}:{slot}:1", revision=1, kind="SlotIssued",
        policy=policy or SlotPolicy(duration_seconds=120, cooldown_seconds=30),
        code_tag=replica.code_tag(issuer, code), effective_at=start,
    )
    result = replica.apply_event(event, authenticated_peer_id=issuer, issued_kind=kind)
    assert result.outcome == "applied"
    return event


def next_event(slot, revision, name, *, when=None, policy=None, tag=None):
    return AccessSlotEvent(
        issuer_id=slot.issuer_id, slot_id=slot.slot_id,
        logical_agent_id=slot.logical_agent_id,
        revision=revision,
        event_id=f"{slot.issuer_id}:{slot.slot_id}:{revision}",
        kind=name, effective_at=when, policy=policy, code_tag=tag,
    )


def apply(replica, event):
    return replica.apply_event(event, authenticated_peer_id=event.issuer_id)


def test_same_code_different_issuers_resolve_unambiguously(replica):
    issued(replica, issuer="firstbyte", slot="fb-1", agent="agent-fb", code="1234")
    issued(replica, issuer="bacloud", slot="bac-1", agent="agent-bac", code="1234")
    fb = replica.attach(issuer_id="firstbyte", code="1234", connection_key="chat:fb-exec")
    bac = replica.attach(issuer_id="bacloud", code="1234", connection_key="chat:bac-coord")
    assert fb.logical_agent_id == "agent-fb"
    assert bac.logical_agent_id == "agent-bac"
    assert replica.may_write(connection_key="chat:fb-exec", now=T0)
    assert replica.may_write(connection_key="chat:bac-coord", now=T0)


def test_different_connector_metadata_bind_same_logical_agent(replica):
    issued(replica, slot="shared", agent="same-agent", code="9876")
    a = replica.attach(issuer_id="bacloud", code="9876", connection_key="opaque-exec-provider-key")
    b = replica.attach(
        issuer_id="bacloud", code="9876", connection_key="opaque-coordinator-provider-key"
    )
    assert a.logical_agent_id == b.logical_agent_id == "same-agent"
    assert replica.may_write(connection_key="opaque-exec-provider-key", now=T0)
    assert replica.may_write(connection_key="opaque-coordinator-provider-key", now=T0)
    assert replica.may_write(connection_key="unattached-other-chat", now=T0) is False


def test_rearm_cooldown_is_locally_determined_without_issuer(replica):
    issued(replica, slot="rearm", policy=SlotPolicy(120, 30), code="4523")
    replica.attach(issuer_id="bacloud", code="4523", connection_key="provider-X")
    assert replica.may_write(connection_key="provider-X", now=T0 + timedelta(seconds=119))
    assert not replica.may_write(connection_key="provider-X", now=T0 + timedelta(seconds=120))
    assert not replica.may_write(connection_key="provider-X", now=T0 + timedelta(seconds=149))
    assert replica.may_write(connection_key="provider-X", now=T0 + timedelta(seconds=150))
    assert replica.may_write(connection_key="provider-X", now=T0 + timedelta(seconds=451))
    assert not replica.may_write(connection_key="provider-X", now=T0 + timedelta(seconds=420))


def test_explicit_issuer_end_starts_cooldown_then_autorearms(replica):
    first = issued(replica, slot="stop")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="A")
    result = apply(replica, next_event(first, 2, "SessionEnded", when=T0 + timedelta(seconds=9)))
    assert result.release_claims and result.logical_agent_id == "agent-1"
    assert not replica.may_write(connection_key="A", now=T0 + timedelta(seconds=10))
    assert not replica.may_write(connection_key="A", now=T0 + timedelta(seconds=38))
    assert replica.may_write(connection_key="A", now=T0 + timedelta(seconds=39))
    # No session.detach call and no repeat AC entry after local rearm.


def test_persistent_end_retains_claims_and_rearms(replica):
    first = issued(replica, kind="persistent")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="A")
    result = apply(replica, next_event(first, 2, "SessionEnded", when=T0 + timedelta(seconds=10)))
    assert result.outcome == "applied"
    assert result.release_claims is False
    assert replica.may_write(connection_key="A", now=T0 + timedelta(seconds=40))


def test_legacy_mobile_slot_delete_releases_claims_and_binding(replica):
    first = issued(replica, slot="legacy-to-delete")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="A")
    result = apply(replica, next_event(first, 2, "SlotDeleted"))
    assert result.release_claims and result.logical_agent_id == "agent-1"
    assert replica.slot("bacloud", "legacy-to-delete").state == "deleted"
    assert not replica.may_write(connection_key="A", now=T0)
    with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
        replica.attach(issuer_id="bacloud", code="1234", connection_key="B")
    with pytest.raises(AccessMeshError, match="access_mesh_deleted_slot"):
        apply(replica, next_event(first, 3, "SlotResumed"))


def test_persistent_delete_releases_even_retained_claims(replica):
    first = issued(replica, slot="persist", kind="persistent")
    assert apply(replica, next_event(first, 2, "SlotDeleted")).release_claims


def test_suspend_disables_write_and_rearm_until_resumed(replica):
    first = issued(replica, slot="suspend")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="chat")
    suspend = next_event(first, 2, "SlotSuspended")
    assert apply(replica, suspend).release_claims
    assert not replica.may_write(connection_key="chat", now=T0 + timedelta(seconds=150))
    with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
        replica.attach(issuer_id="bacloud", code="1234", connection_key="other")
    resume = next_event(first, 3, "SlotResumed")
    assert not apply(replica, resume).release_claims
    assert replica.may_write(connection_key="chat", now=T0 + timedelta(seconds=150))


def test_policy_update_changes_local_timeout_and_rearm(replica):
    first = issued(replica, slot="policy", policy=SlotPolicy(10, 10), code="1234")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="my-actor")
    assert not replica.may_write(connection_key="my-actor", now=T0 + timedelta(seconds=11))
    event = next_event(
        first, 2, "SlotPolicyChanged", policy=SlotPolicy(
            duration_seconds=60, cooldown_seconds=30,
            rearm_enabled=False, warning_seconds=5, draining_seconds=5,
        ),
    )
    apply(replica, event)
    assert replica.may_write(connection_key="my-actor", now=T0 + timedelta(seconds=11))
    assert not replica.may_write(connection_key="my-actor", now=T0 + timedelta(seconds=60))


def test_rotated_code_required_for_new_attach_existing_binding_survives(replica):
    first = issued(replica, slot="rotate", code="1234")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="existing")
    apply(replica, next_event(
        first, 2, "AccessCodeRotated", tag=replica.code_tag("bacloud", "5678"),
    ))
    with pytest.raises(AccessMeshError, match="access_mesh_slot_not_found"):
        replica.attach(issuer_id="bacloud", code="1234", connection_key="new")
    new = replica.attach(issuer_id="bacloud", code="5678", connection_key="new")
    assert new.logical_agent_id == "agent-1"
    assert replica.may_write(connection_key="existing", now=T0)


def test_event_order_and_replay_are_safe(replica):
    first = issued(replica, slot="order")
    duplicate = replica.apply_event(first, authenticated_peer_id="bacloud")
    assert duplicate.outcome == "duplicate"
    suspend = next_event(first, 2, "SlotSuspended")
    apply(replica, suspend)
    assert replica.apply_event(first, authenticated_peer_id="bacloud").outcome == "stale"
    with pytest.raises(AccessMeshError, match="access_mesh_event_conflict"):
        apply(replica, AccessSlotEvent(
            issuer_id="bacloud", slot_id="order", logical_agent_id="agent-1",
            revision=2, event_id="different-event", kind="SlotResumed",
        ))
    with pytest.raises(AccessMeshError, match="access_mesh_event_gap"):
        apply(replica, next_event(first, 4, "SlotResumed"))
    assert replica.slot("bacloud", "order").revision == 2


def test_only_authenticated_issuer_can_change_slot(replica):
    first = issued(replica)
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_issuer"):
        replica.apply_event(next_event(first, 2, "SlotDeleted"), authenticated_peer_id="firstbyte")
    assert replica.slot("bacloud", "slot-1").state == "active"


def test_unknown_slot_cannot_be_created_by_update(replica):
    event = AccessSlotEvent(
        issuer_id="bacloud", slot_id="unknown", logical_agent_id="agent",
        revision=1, event_id="missing", kind="SlotResumed",
    )
    with pytest.raises(AccessMeshError, match="access_mesh_unknown_slot"):
        apply(replica, event)


def test_existing_connector_cannot_be_rebound_to_another_agent(replica):
    issued(replica, slot="one", code="1111")
    issued(replica, slot="two", agent="other", code="2222")
    replica.attach(issuer_id="bacloud", code="1111", connection_key="same-provider")
    with pytest.raises(AccessMeshError, match="access_mesh_binding_conflict"):
        replica.attach(issuer_id="bacloud", code="2222", connection_key="same-provider")


def test_local_gate_remains_usable_after_process_restart(tmp_path):
    path = tmp_path / "durable.db"
    kwargs = dict(
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )
    original = LocalAccessMesh(path, **kwargs)
    issued(original, slot="durable", code="1742")
    original.attach(issuer_id="bacloud", code="1742", connection_key="provider-1")
    restarted = LocalAccessMesh(path, **kwargs)
    assert restarted.may_write(connection_key="provider-1", now=T0)
    assert not restarted.may_write(connection_key="provider-1", now=T0 + timedelta(seconds=130))
    assert restarted.may_write(connection_key="provider-1", now=T0 + timedelta(seconds=150))


def test_requires_valid_code_time_policy_and_trusted_peer(replica):
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_code"):
        replica.code_tag("bacloud", "12345")
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_policy"):
        SlotPolicy(duration_seconds=30, warning_seconds=30)
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_time"):
        AccessSlotEvent(
            issuer_id="bacloud", slot_id="slot", logical_agent_id="agent",
            event_id="evt", revision=1, kind="SessionStarted",
            effective_at=datetime(2026, 10, 8, 10, 0),
        )
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_issuer"):
        replica.apply_event(
            AccessSlotEvent(
                issuer_id="tokyo", slot_id="slot", logical_agent_id="agent",
                event_id="evt", revision=1, kind="SlotIssued",
                policy=SlotPolicy(10), code_tag=replica.code_tag("tokyo", "1234"),
            ),
            authenticated_peer_id="tokyo", issued_kind="legacy",
        )
