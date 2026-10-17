"""Native identities for focused storage and command-port tests."""

from datetime import UTC, datetime

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.human_names import number_agent_name
from terminal_mcp.storage.access_mesh import AccessMeshStore


def native_agent(
    path,
    agent_id="test-agent",
    *,
    node_id="local",
    code="1234",
    now=None,
    session_id=None,
    session_epoch=1,
    policy=None,
):
    now = now or datetime.now(UTC)
    store = AccessMeshStore(
        path,
        local_node_id=node_id,
        trusted_issuers=frozenset({node_id}),
        proof_key=b"native-runtime-unit-tests-proof-only",
        clock=lambda: now,
    )
    slot_id = "test-slot-" + agent_id
    store.apply_event(
        AccessSlotEvent(
            node_id,
            slot_id,
            agent_id,
            1,
            "test-issue-" + agent_id,
            "SlotIssued",
            policy=policy or SlotPolicy(1380, 180),
            code_tag=store.code_tag(node_id, code),
            effective_at=now,
            agent_name=number_agent_name(code),
        ),
        authenticated_peer_id=node_id,
        issued_kind="persistent",
        mutation_number={"number": code, "attempt_id": "test-" + agent_id, "started_at": now},
    )
    identity = store.local_identity(store.slot(node_id, slot_id), now=now)
    if session_id is not None:
        with store._connect() as db:
            db.execute(
                "UPDATE logical_agent_work_sessions SET work_session_id=?,session_epoch=? "
                "WHERE work_session_id=?",
                (session_id, session_epoch, identity["work_session_id"]),
            )
            db.execute(
                "UPDATE access_mesh_local_sessions SET work_session_id=?,session_epoch=? "
                "WHERE issuer_id=? AND slot_id=?",
                (session_id, session_epoch, node_id, slot_id),
            )
        identity.update(work_session_id=session_id, session_epoch=session_epoch)
    return store, identity
