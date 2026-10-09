"""Current public Access session shape and task-claim lifecycle after an explicit end.

Persistent grants are an internal/operator policy; the public Access tool no
longer accepts mode or issuer_node_id. Do not confuse old public metadata
with internal TaskStore claim ownership.
"""

import asyncio

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.storage.tasks import TaskStore


def test_public_access_never_accepts_persistent_mode_or_issuer_field(tmp_path):
    config = settings(tmp_path)
    namespace, task_id = "qa3-access-contract", "retained"
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        legacy = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "persistent", "session_number": "1234"},
            request_id=801,
        )
        assert legacy["ok"] is False
        assert legacy["error"]["code"] == "input_validation_failed"

        issued = call(client, "access", "session", {"action": "start"}, request_id=802)
        assert set(issued) == {"ok", "session_number"} and issued["ok"], issued
        attach = {"session_number": issued["session_number"]}
        for role in ("executor", "coordinator"):
            assert call(client, role, "session", attach) == {"ok": True}
            invalid = call(
                client,
                role,
                "session",
                {**attach, "issuer_node_id": "firstbyte"},
                request_id=803,
            )
            assert invalid["ok"] is False
            assert invalid["error"]["code"] == "input_validation_failed"

        created = call(
            client,
            "coordinator",
            "task_manage",
            {
                "action": "create",
                "namespace": namespace,
                "task_id": task_id,
                "title": "Public Access claim lifecycle",
                "isolation_hint": "none",
            },
            request_id=804,
        )
        assert created["ok"], created
        claimed = call(
            client,
            "executor",
            "task_claim",
            {
                "action": "claim",
                "namespace": namespace,
                "task_id": task_id,
                "claim_intent": "Validate task ownership on public Access end",
            },
            request_id=805,
        )
        assert claimed["ok"], claimed
        before = asyncio.run(TaskStore(config.database_path).active_claims(namespace, task_id))
        assert len(before) == 1
        # Public sessions cannot leak identity or claim ownership internals.
        assert "logical_agent_id" not in issued

        ended = call(client, "access", "session", {"action": "end"}, request_id=806)
        assert ended == {"ok": True}
        after = asyncio.run(TaskStore(config.database_path).get_task(namespace, task_id))
        assert after["state"] == "ready"
        assert after["revision"] == 1
