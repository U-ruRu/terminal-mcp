"""HTTP command acceptance executes actual Linux shell processes."""

import time

from auth_http_test_helpers import bearer_client as bearer_client
from auth_http_test_helpers import bearer_session as bearer_session


def test_bearer_command_run_and_read(bearer_session):
    client, headers, agent_id = bearer_session
    run = client.post(
        "/actions/run",
        json={"agent_id": agent_id, "cmd": "printf ok", "task_scope": "none"},
        headers=headers,
    )
    assert run.status_code == 200 and run.json()["ok"] is True
    cmd_hash = run.json()["cmd_hash"]
    for _ in range(100):
        result = client.post(
            "/actions/read",
            json={"agent_id": agent_id, "cmd_hash": cmd_hash, "lines_count": 100, "offset": 0},
            headers=headers,
        ).json()
        if result["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert result["status"] == "completed"
    global_read = client.post("/actions/read", json={}, headers=headers)
    assert global_read.status_code == 200
    assert global_read.json()["agent_name"] == "anonymous"
    command_only_read = client.post("/actions/read", json={"cmd_hash": cmd_hash}, headers=headers)
    assert command_only_read.status_code == 200
    assert command_only_read.json()["status"] == "completed"


def test_bearer_recovery_attribution(bearer_session):
    client, headers, agent_id = bearer_session
    recovery = client.post(
        "/actions/recovery",
        json={"agent_id": agent_id, "cmd": "printf action-recovery"},
        headers=headers,
    ).json()
    assert recovery["ok"] is True
    assert recovery["agent_name"] == agent_id.rsplit("-", 1)[0]
    assert recovery["lines"][0].endswith("action-recovery")
    anonymous_recovery = client.post(
        "/actions/recovery", json={"cmd": "printf anonymous-action-recovery"}, headers=headers
    ).json()
    assert anonymous_recovery["ok"] is True
    assert anonymous_recovery["agent_name"] == "anonymous"
    assert anonymous_recovery["lines"][0].endswith("anonymous-action-recovery")
