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


def issued(
    replica,
    issuer="bacloud",
    slot="slot-1",
    agent="agent-1",
    code="1234",
    kind="persistent",
    policy=None,
    start=T0,
):
    event = AccessSlotEvent(
        issuer_id=issuer,
        slot_id=slot,
        logical_agent_id=agent,
        event_id=f"{issuer}:{slot}:1",
        revision=1,
        kind="SlotIssued",
        policy=policy or SlotPolicy(duration_seconds=120, cooldown_seconds=30),
        code_tag=replica.code_tag(issuer, code),
        effective_at=start,
    )
    result = replica.apply_event(event, authenticated_peer_id=issuer, issued_kind=kind)
    assert result.outcome == "applied"
    return event


def next_event(slot, revision, name, *, when=None, policy=None, tag=None):
    return AccessSlotEvent(
        issuer_id=slot.issuer_id,
        slot_id=slot.slot_id,
        logical_agent_id=slot.logical_agent_id,
        revision=revision,
        event_id=f"{slot.issuer_id}:{slot.slot_id}:{revision}",
        kind=name,
        effective_at=when,
        policy=policy,
        code_tag=tag,
    )


def apply(replica, event):
    return replica.apply_event(event, authenticated_peer_id=event.issuer_id)


def test_connector_attachment_resolves_shared_logical_agent(replica):
    issued(replica, slot="shared", agent="same-agent", code="9876")
    first = replica.attach(issuer_id="bacloud", code="9876", connection_key="executor")

    second = replica.attach(issuer_id="bacloud", code="9876", connection_key="coordinator")

    assert second.logical_agent_id == first.logical_agent_id == "same-agent"

    # No session.detach call and no repeat AC entry after local rearm.


def test_persistent_end_retains_claims(replica):
    first = issued(replica, kind="persistent")

    result = apply(replica, next_event(first, 2, "SessionEnded", when=T0 + timedelta(seconds=10)))

    assert result.outcome == "applied"
    assert result.release_claims is False


def test_persistent_delete_releases_even_retained_claims(replica):
    first = issued(replica, slot="persist", kind="persistent")
    assert apply(replica, next_event(first, 2, "SlotDeleted")).release_claims


def test_suspension_fences_existing_attachment(replica):
    first = issued(replica, slot="suspend", kind="persistent")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="chat")

    apply(replica, next_event(first, 2, "SlotSuspended"))

    assert not replica.may_write(connection_key="chat", now=T0)


def test_rotation_preserves_existing_attachment(replica):
    first = issued(replica, slot="rotate", code="1234", kind="persistent")
    replica.attach(issuer_id="bacloud", code="1234", connection_key="existing")

    apply(
        replica, next_event(first, 2, "AccessCodeRotated", tag=replica.code_tag("bacloud", "5678"))
    )

    assert replica.may_write(connection_key="existing", now=T0)


def test_conflicting_event_replay_preserves_slot(replica):
    from dataclasses import replace

    first = issued(replica, slot="order", kind="persistent")
    altered = replace(first, code_tag=replica.code_tag("bacloud", "5678"))

    with pytest.raises(AccessMeshError, match="access_mesh_event_conflict"):
        replica.apply_event(altered, authenticated_peer_id="bacloud")

    assert replica.slot("bacloud", "order").revision == 1


def test_only_authenticated_issuer_can_change_slot(replica):
    first = issued(replica)
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_issuer"):
        replica.apply_event(next_event(first, 2, "SlotDeleted"), authenticated_peer_id="firstbyte")
    assert replica.slot("bacloud", "slot-1").state == "active"


def test_existing_connector_cannot_be_rebound_to_another_agent(replica):
    issued(replica, slot="one", code="1111")
    issued(replica, slot="two", agent="other", code="2222")
    replica.attach(issuer_id="bacloud", code="1111", connection_key="same-provider")
    with pytest.raises(AccessMeshError, match="access_mesh_binding_conflict"):
        replica.attach(issuer_id="bacloud", code="2222", connection_key="same-provider")


def test_attachment_survives_store_reopen(tmp_path):
    path = tmp_path / "durable.db"
    kwargs = dict(
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=KEY,
    )
    original = LocalAccessMesh(path, **kwargs)
    issued(original, slot="durable", code="1742", kind="persistent")
    original.attach(issuer_id="bacloud", code="1742", connection_key="provider-1")

    restarted = LocalAccessMesh(path, **kwargs)

    assert restarted.may_write(connection_key="provider-1", now=T0)
