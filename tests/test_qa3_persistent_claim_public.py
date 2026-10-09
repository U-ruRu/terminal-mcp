"""QA regression: persistent Access Mesh claim ownership must survive own cycle end."""
import asyncio
import sqlite3

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings
from test_access_mesh_operator_http import mutate

from terminal_mcp.app import create_app
from terminal_mcp.storage.tasks import TaskStore


def test_persistent_public_role_claim_never_uses_session_lease(tmp_path):
    config = settings(tmp_path)
    db = config.database_path
    ns = "qa3-persistent-claim"
    task_id = "retained"
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        provisioned = mutate(
            client, "create", "qa3-persistent-create", mode="persistent"
        )
        assert provisioned["ok"], provisioned
        code = provisioned["access_code"]
        issued = call(
            client, "access", "session",
            {"action": "start", "mode": "persistent", "session_number": code},
            request_id=801, conversation="qa3-access",
        )
        assert issued["ok"], issued
        binding = {"issuer_node_id": "firstbyte", "session_number": code}
        attached_executor = call(
            client, "executor", "session", binding,
            request_id=802, conversation="qa3-executor",
        )
        attached_coord = call(
            client, "coordinator", "session", binding,
            request_id=803, conversation="qa3-coordinator",
        )
        assert "logical_agent_id" not in issued  # Internal issuer id is private
        assert attached_executor["logical_agent_id"] == attached_coord["logical_agent_id"]
        durable_owner = attached_executor["logical_agent_id"]

        created = call(
            client, "coordinator", "task_manage",
            {"action": "create", "namespace": ns, "task_id": task_id,
             "title": "Persistent claim lifetime regression", "isolation_hint": "none"},
            request_id=804, conversation="qa3-coordinator",
        )
        assert created["ok"], created
        claimed = call(
            client, "executor", "task_claim",
            {"action": "claim", "namespace": ns, "task_id": task_id,
             "claim_intent": "Retain across persistent session end"},
            request_id=805, conversation="qa3-executor",
        )
        assert claimed["ok"], claimed

        claims_before = asyncio.run(TaskStore(db).active_claims(ns, task_id))
        assert len(claims_before) == 1, claims_before
        assert claims_before[0]["owner_id"] == durable_owner
        with sqlite3.connect(db) as store:
            active = store.execute(
                """SELECT c.id, l.work_session_id
                   FROM work_claims AS c
                   LEFT JOIN work_claim_leases AS l ON l.claim_id=c.id
                   WHERE c.namespace=? AND c.task_id=? AND c.released_at IS NULL""",
                (ns, task_id),
            ).fetchall()
        assert len(active) == 1
        assert active[0][1] is None, "Persistent policy must never create WorkSession lease"

        ended = call(
            client, "access", "session", {"action": "end"},
            request_id=806, conversation="qa3-access",
        )
        assert ended["ok"], ended
        claims_after = asyncio.run(TaskStore(db).active_claims(ns, task_id))
        assert len(claims_after) == 1, claims_after
        assert claims_after[0]["owner_id"] == durable_owner

