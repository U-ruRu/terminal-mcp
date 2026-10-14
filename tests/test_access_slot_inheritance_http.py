"""Real operator HTTP -> policy storage -> current-window/replica inheritance.

All applications, identities and codes are synthetic and held in temporary files.
Mutation receipts containing codes are deliberately omitted from assertion messages.
"""

from copy import deepcopy
from datetime import timedelta
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS, internal_persistent_start
from test_android_slot_http_window_qa import T0, read_slot
from test_android_slot_policy_continuation import set_clock

from terminal_mcp.app import create_app


def mutate(client, action, key, **values):
    if (
        action == "policy"
        and values.get("inherit_policy") is True
        and "expected_parent_revision" not in values
    ):
        values["expected_parent_revision"] = defaults(client)["revision"]
    response = client.post(
        "/actions/access/mutate",
        headers=HEADERS,
        json={"action": action, "idempotency_key": key, **values},
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("ok") is True
    return body


def defaults(client):
    response = client.get("/actions/access/defaults", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body.get("ok") is True
    return body


def create(client, key, **kwargs):
    return mutate(client, "create", key, mode="persistent", **kwargs)


def change_defaults(client, key, **kwargs):
    return mutate(client, "defaults", key, expected_revision=defaults(client)["revision"], **kwargs)


def mesh_config(tmp_path, control="firstbyte"):
    config = settings(tmp_path)
    return type(config).model_validate(
        {
            **config.model_dump(),
            "fleet_id": "qa-inheritance",
            "fleet_v1_authority_enabled": True,
            "fleet_v1_source_enabled": True,
            "fleet_node_id": "firstbyte",
            "fleet_peer_transports_path": str(tmp_path / "peer-transports.json"),
            "mesh_vpn_state_dir": str(tmp_path / "mesh-vpn"),
            "fleet_control_node_id": control,
            "fleet_control_path": tmp_path / "fleet.sqlite3",
        }
    )


def fleet_policy(client, duration, *, expected=None):
    state = client.get("/actions/fleet/control", headers=HEADERS).json()["control"]
    response = client.post(
        "/actions/fleet/control/policy",
        headers=HEADERS,
        json={
            "duration_seconds": duration,
            "warning_after_seconds": duration - 180,
            "alert_after_seconds": duration - 60,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": True,
            "expected_revision": state["policy"]["revision"] if expected is None else expected,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_standalone_defaults_duration_updates_inherited_slot_after_session_start(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "defaults-after-start")
        assert internal_persistent_start(client, app, issued["access_code"])["ok"]
        change_defaults(client, "defaults-3h", policy={"duration_seconds": 10800})
        assert read_slot(client, issued["slot_id"])["policy"]["duration_seconds"] == 10800


def test_standalone_defaults_edit_does_not_restart_active_session(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "defaults-active-window")
        assert internal_persistent_start(client, app, issued["access_code"])["ok"]
        before = read_slot(client, issued["slot_id"])
        change_defaults(client, "defaults-duration-change", policy={"duration_seconds": 10800})
        assert (
            read_slot(client, issued["slot_id"])["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )


def test_standalone_slot_override_reset_uses_current_defaults(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "slot-reset", policy={"duration_seconds": 7200})
        before = read_slot(client, issued["slot_id"])
        parent = defaults(client)
        mutate(
            client,
            "policy",
            "restore-standalone-defaults",
            slot_id=issued["slot_id"],
            expected_revision=before["revision"],
            expected_parent_revision=parent["revision"],
            inherit_policy=True,
        )
        result = read_slot(client, issued["slot_id"])
        assert result["policy_override"] is None
        assert result["policy"] == parent["policy"]


def test_real_fleet_http_policy_reaches_native_slots_and_reset(tmp_path):
    app = create_app(mesh_config(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        adopted = client.post(
            "/actions/fleet/control/adopt",
            headers=HEADERS,
            json={"mesh_id": "qa-mesh", "display_name": "QA"},
        ).json()
        assert adopted.get("ok") is True
        assert app.state.managed_fleet_control.native_access is app.state.access_mesh
        initial = defaults(client)
        assert initial["mesh_policy_supported"] is True
        assert initial["policy_source"] == "mesh"
        issued = create(client, "mesh-inherited")
        assert internal_persistent_start(client, app, issued["access_code"]).get("ok") is True
        before = read_slot(client, issued["slot_id"])
        set_clock(app, T0 + timedelta(seconds=60))
        assert fleet_policy(client, 10800).get("ok") is True
        inherited = read_slot(client, issued["slot_id"])
        assert inherited["policy"]["duration_seconds"] == 10800
        assert inherited["policy"]["warning_seconds"] == 180
        assert inherited["inheritance"]["source"] == "mesh"
        assert inherited["inheritance"]["parent"]["source"] == "server"
        assert inherited["inheritance"]["parent"]["id"] == "firstbyte"
        assert inherited["inheritance"]["parent"]["revision"] == defaults(client)["revision"]
        assert (
            inherited["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )
        change_defaults(client, "server-override", policy={"duration_seconds": 14400})
        assert fleet_policy(client, 18000).get("ok") is True
        assert read_slot(client, issued["slot_id"])["policy"]["duration_seconds"] == 14400
        assert defaults(client)["inherited_policy"]["duration_seconds"] == 18000
        change_defaults(client, "server-reset", inherit_policy=True)
        reset = read_slot(client, issued["slot_id"])
        assert reset["policy"]["duration_seconds"] == 18000
        assert reset["inheritance"]["source"] == "mesh"
        assert (
            reset["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )
        mutate(
            client,
            "policy",
            "mesh-slot-override",
            slot_id=issued["slot_id"],
            expected_revision=reset["revision"],
            policy={"duration_seconds": 7200},
        )
        fixed = read_slot(client, issued["slot_id"])
        assert fleet_policy(client, 21600).get("ok") is True
        assert read_slot(client, issued["slot_id"])["policy"] == fixed["policy"]
        assert fleet_policy(client, 25200, expected=1).get("ok") is False
        mutate(
            client,
            "policy",
            "mesh-slot-reset",
            slot_id=issued["slot_id"],
            expected_revision=fixed["revision"],
            inherit_policy=True,
        )
        assert read_slot(client, issued["slot_id"])["policy"]["duration_seconds"] == 21600


def test_applied_mesh_replica_and_detach_keep_current_snapshot(tmp_path):
    from test_fleet_control_hardening import _managed_replica_snapshot

    app = create_app(mesh_config(tmp_path, control="control"))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        controller = app.state.managed_fleet_control
        snapshot = _managed_replica_snapshot(include_member_mesh_id=True, member_mesh_id="mesh-a")
        snapshot["fleet_id"] = "qa-inheritance"
        snapshot["nodes"][1]["node_id"] = "firstbyte"
        snapshot["policy"]["duration_seconds"] = 10800
        snapshot["_peer_material"] = [{"node_id": "control", "auth_token": "synthetic-test-peer"}]
        client.portal.call(partial(controller.apply_replica, snapshot, source_node_id="control"))
        assert defaults(client)["policy"]["duration_seconds"] == 10800
        assert defaults(client)["inheritance"]["parent"]["freshness"] == "last_known"
        issued = create(client, "replica-inherited")
        assert internal_persistent_start(client, app, issued["access_code"]).get("ok") is True
        before = read_slot(client, issued["slot_id"])
        detached = deepcopy(snapshot)
        detached["mesh"] = None
        detached["nodes"][1]["mesh_id"] = None
        detached["revisions"] = {"topology": 3, "trust": 3, "access_policy": 2}
        client.portal.call(partial(controller.apply_replica, detached, source_node_id="control"))
        after = read_slot(client, issued["slot_id"])
        assert defaults(client)["mesh_policy_supported"] is False
        assert after["policy"]["duration_seconds"] == settings(tmp_path).access_mesh_duration_sec
        assert (
            after["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )
        assert after["inheritance"]["source"] == "standalone"


@pytest.mark.parametrize(
    "failure_type,expected_code",
    [
        (httpx.ConnectTimeout, "control_authority_unavailable"),
        (httpx.ReadTimeout, "control_mutation_unconfirmed"),
    ],
)
def test_unavailable_mesh_authority_is_not_a_success_or_local_override(
    tmp_path, failure_type, expected_code
):
    from test_fleet_control_hardening import _managed_replica_snapshot

    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise failure_type("synthetic authority unavailable")

        async def get(self, *args, **kwargs):
            raise failure_type("synthetic authority unavailable")

    app = create_app(mesh_config(tmp_path, control="control"))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        controller = app.state.managed_fleet_control
        snapshot = _managed_replica_snapshot(include_member_mesh_id=True, member_mesh_id="mesh-a")
        snapshot["fleet_id"] = "qa-inheritance"
        snapshot["nodes"][1]["node_id"] = "firstbyte"
        snapshot["_peer_material"] = [{"node_id": "control", "auth_token": "synthetic-test-peer"}]
        controller.client_factory = OfflineClient
        client.portal.call(partial(controller.apply_replica, snapshot, source_node_id="control"))
        before = defaults(client)
        response = fleet_policy(client, 10800)
        assert response.get("ok") is False
        assert response.get("code") == expected_code
        assert defaults(client) == before
        assert before["inheritance"]["parent"]["freshness"] == "last_known"
        assert before["policy_override"] is None


def test_reset_request_requires_explicit_parent_revision(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "parent-required")
        response = client.post(
            "/actions/access/mutate",
            headers=HEADERS,
            json={
                "action": "policy",
                "slot_id": issued["slot_id"],
                "expected_revision": 1,
                "inherit_policy": True,
                "idempotency_key": "missing-parent-proof",
            },
        )
        assert response.status_code == 200
        assert response.json().get("ok") is False
        assert response.json().get("code") == "validation_error"
        assert read_slot(client, issued["slot_id"])["revision"] == 1
