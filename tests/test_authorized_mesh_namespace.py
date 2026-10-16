"""Synthetic Join/Detach/Switch across namespaces; no live credentials or paths."""

import os
import sqlite3
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_access_slot_inheritance_http import mesh_config
from test_fleet_control_hardening import _managed_replica_snapshot

from terminal_mcp.app import create_app
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore
from terminal_mcp.fleet.namespace import finish_namespace_transition


@pytest.fixture(autouse=True)
def portable_mode(monkeypatch):
    # Production is Linux. Windows lacks fchmod; emulate it only for temp fixtures.
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)


def snapshot(namespace="destination", controller="control", member="firstbyte"):
    state = _managed_replica_snapshot(include_member_mesh_id=True, member_mesh_id="mesh-a")
    state["fleet_id"] = namespace
    state["control_node_id"] = controller
    state["nodes"][0]["node_id"] = controller
    state["nodes"][1]["node_id"] = member
    return state


def peer_apply(client, state, token):
    return client.post("/internal/fleet/control/apply", json={"state": state}, headers={
        "x-terminal-mcp-peer": state["control_node_id"], "authorization": "Bearer " + token,
    })


def enrollment(client):
    response = client.get("/actions/fleet/control/enrollment", headers={"authorization": "Bearer mesh-test-token"})
    assert response.status_code == 200
    return response.json()["enrollment"]


def material(state, local):
    state["_peer_material"] = [
        {"node_id": "firstbyte", "auth_token": local["auth_token"]},
        {"node_id": state["control_node_id"], "auth_token": "synthetic-controller-token-" + "x" * 48},
    ]
    state["nodes"][1]["public_key"] = local["public_key"]


def test_http_different_namespace_requires_operator_and_peer_credentials(tmp_path):
    config = mesh_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        assert client.get("/actions/fleet/control/enrollment").status_code == 401
        local = enrollment(client)
        state = snapshot()
        material(state, local)
        for namespace in (config.fleet_id, "destination"):
            state["fleet_id"] = namespace
            assert peer_apply(client, state, "unauthorized").status_code == 401
            assert app.state.fleet_control.fleet_id == "qa-inheritance"
        state["fleet_id"] = "destination"
        forged = deepcopy(state)
        forged["_peer_material"][0]["auth_token"] = "incorrect-enrollment"
        assert peer_apply(client, forged, local["auth_token"]).status_code == 409
        assert app.state.fleet_control.fleet_id == "qa-inheritance"
        response = peer_apply(client, state, local["auth_token"])
        assert response.status_code == 200 and response.json()["ok"] is True
        assert app.state.fleet_control.fleet_id == config.fleet_id == "destination"
        assert app.state.access_mesh.store.managed_proof_ready
        assert 'TERMINAL_MCP_FLEET_ID="destination"' in config.env_file_path.read_text()
        with sqlite3.connect(config.effective_fleet_node_meta_path()) as db:
            assert db.execute("SELECT fleet_id FROM source_meta").fetchone()[0] == "destination"

    # Startup with old input recovers only an explicitly committed pending intent;
    # normal restart loads the durable namespace from configuration.
    restarted = config.model_copy()
    with TestClient(create_app(restarted), base_url="https://terminal.example") as client:
        assert enrollment(client)["node_id"] == "firstbyte"
        assert restarted.fleet_id == "destination"


@pytest.mark.asyncio
async def test_raw_namespace_snapshot_cannot_grant_authorization(tmp_path):
    store = FleetControlStore(tmp_path / "control.sqlite3", fleet_id="home", node_id="firstbyte", control_node_id="firstbyte")
    await store.initialize()
    state = snapshot()
    state["namespace_authorized"] = True
    with pytest.raises(FleetControlError, match="namespace_transition_authorization_required"):
        await store.apply_managed_replica(state)
    assert store.fleet_id == "home" and await store.pending_namespace() is None


def test_join_detach_switch_restores_home_and_preserves_node_identity(tmp_path):
    config = mesh_config(tmp_path)
    config.env_file_path.write_text('TERMINAL_MCP_FLEET_ID="qa-inheritance"\nTERMINAL_MCP_FLEET_ID="stale-duplicate"\nOTHER="synthetic"\n')
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        before = enrollment(client)
        identity = client.portal.call(app.state.fleet_control.managed_identity)
        source = client.portal.call(app.state.fleet_node_meta.get)
        joined = snapshot()
        material(joined, before)
        assert peer_apply(client, joined, before["auth_token"]).json()["ok"] is True
        # A second authority cannot steal an attached node, even with the same
        # enrollment token and a public namespace copied from its controller.
        stolen = snapshot("other", "other-control")
        material(stolen, before)
        assert peer_apply(client, stolen, before["auth_token"]).status_code in (401, 409)
        assert config.fleet_id == "destination"
        detached = deepcopy(joined)
        detached["nodes"][1].update(state="draining", mesh_id=None)
        for key in detached["revisions"]:
            detached["revisions"][key] += 1
        async def released(*args, **kwargs):
            return {"ok": True}
        app.state.managed_fleet_control._forward_mutation_to = released
        assert peer_apply(client, detached, before["auth_token"]).json()["ok"] is True
        assert config.fleet_id == app.state.fleet_control.fleet_id == "qa-inheritance"
        assert app.state.fleet_control.control_node_id == "firstbyte"
        assert not app.state.access_mesh.store.managed_proof_ready
        after_detach = enrollment(client)
        switched = snapshot("other", "other-control")
        material(switched, after_detach)
        assert peer_apply(client, switched, after_detach["auth_token"]).json()["ok"] is True
        assert config.fleet_id == "other"
        after = client.portal.call(app.state.fleet_control.managed_identity)
        assert (after["private_key"], after["public_key"], after["generation"]) == (
            identity["private_key"], identity["public_key"], identity["generation"])
        assert client.portal.call(app.state.fleet_node_meta.get).source_stream_generation == source.source_stream_generation
        persisted = config.env_file_path.read_text()
        assert persisted.count("TERMINAL_MCP_FLEET_ID=") == 1 and 'OTHER="synthetic"' in persisted


def test_active_controller_cannot_be_rehomed_by_enrollment_credential(tmp_path):
    config = mesh_config(tmp_path)
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        response = client.post("/actions/fleet/control/adopt", headers={"authorization": "Bearer mesh-test-token"},
                               json={"mesh_id": "owned-mesh", "display_name": "Owned"})
        assert response.json()["ok"] is True
        local = enrollment(client)
        state = snapshot()
        material(state, local)
        assert peer_apply(client, state, local["auth_token"]).status_code == 409
        assert config.fleet_id == "qa-inheritance"


@pytest.mark.asyncio
async def test_committed_namespace_recovers_after_config_write_failure(tmp_path):
    config = mesh_config(tmp_path)
    store = FleetControlStore(config.effective_fleet_control_path(), fleet_id=config.fleet_id, node_id="firstbyte", control_node_id="firstbyte")
    await store.initialize()
    await store.apply_managed_replica(snapshot(), namespace_authorized=True)
    original = config.env_file_path
    config.env_file_path = tmp_path  # A directory makes the local config writer fail.
    with pytest.raises(OSError):
        finish_namespace_transition(config)
    assert await store.pending_namespace() == "destination"
    config.env_file_path = original
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        assert enrollment(client)["node_id"] == "firstbyte"
        assert config.fleet_id == "destination"
        assert await store.pending_namespace() is None


@pytest.mark.asyncio
async def test_invalid_snapshot_cannot_commit_namespace_or_journal(tmp_path):
    store = FleetControlStore(tmp_path / "control.sqlite3", fleet_id="home", node_id="firstbyte", control_node_id="firstbyte")
    await store.initialize()
    state = snapshot()
    state["policy"]["duration_seconds"] = 0
    with pytest.raises(ValueError, match="invalid access policy"):
        await store.apply_managed_replica(state, namespace_authorized=True)
    assert store.fleet_id == "home" and await store.pending_namespace() is None
