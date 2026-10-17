"""Actual auth middleware gates Mesh decisions independently of agent auth mode."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.auth.middleware import AuthMiddleware
from terminal_mcp.http.fleet_control import build_fleet_control_router


class PairingProbe:
    active = True

    async def active_device_for_client(self, client_id):
        return {"device_id": "device"} if self.active and client_id == "paired-app" else None


class AuthProbe:
    credentials = None

    def bearer_valid(self, token):
        return token == "static-admin"

    async def verify_access(self, token, scopes):
        if token not in {"paired", "unpaired", "read-only"}:
            raise PermissionError("invalid_token")
        granted = {"terminal:read"}
        if token != "read-only":
            granted.add("terminal:execute")
        if not set(scopes) <= granted:
            raise PermissionError("scope_required")
        return {
            "sub": "unpaired-client" if token == "unpaired" else "paired-app",
            "scope": " ".join(sorted(granted)),
        }


class ControlProbe:
    def __init__(self):
        self.calls = []

    async def snapshot(self):
        return {"node_id": "node"}

    async def enrollment_descriptor(self):
        self.calls.append("enrollment")
        return {"node_id": "node", "auth_token": "one-use-test-grant"}

    async def change_primary(self, **_):
        self.calls.append("primary")
        return {"node_id": "node", "control_node_id": "next"}


def protected_app(mode):
    app = FastAPI()
    controller = ControlProbe()
    pairing = PairingProbe()
    app.include_router(build_fleet_control_router(controller, None))
    app.add_middleware(
        AuthMiddleware,
        settings=SimpleNamespace(mode_for=lambda _: mode, public_base_url="https://node.test"),
        auth_service=AuthProbe(),
        pairing_store=pairing,
    )
    return app, controller, pairing


@pytest.mark.parametrize("mode", ["none", "bearer", "oauth"])
@pytest.mark.parametrize("token", [None, "static-admin", "unpaired"])
async def test_unpaired_clients_cannot_enroll_or_change_primary(mode, token):
    app, controller, _ = protected_app(mode)
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://node.test"
    ) as client:
        enrollment = await client.get("/actions/fleet/control/enrollment", headers=headers)
        primary = await client.post(
            "/actions/fleet/control/primary",
            headers=headers,
            json={"node_id": "next", "operation_id": "paired-only"},
        )
    assert enrollment.status_code == primary.status_code == 401
    assert controller.calls == []


@pytest.mark.parametrize("mode", ["none", "bearer", "oauth"])
async def test_current_paired_device_can_enroll_and_manage_mesh(mode):
    app, controller, pairing = protected_app(mode)
    headers = {"Authorization": "Bearer paired"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://node.test"
    ) as client:
        enrollment = await client.get("/actions/fleet/control/enrollment", headers=headers)
        primary = await client.post(
            "/actions/fleet/control/primary",
            headers=headers,
            json={"node_id": "next", "operation_id": "paired-only"},
        )
        pairing.active = False
        revoked = await client.get("/actions/fleet/control/enrollment", headers=headers)
    assert enrollment.status_code == primary.status_code == 200
    assert revoked.status_code == 401
    assert controller.calls == ["enrollment", "primary"]


async def test_paired_read_scope_cannot_issue_a_topology_credential():
    app, controller, _ = protected_app("oauth")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://node.test"
    ) as client:
        response = await client.get(
            "/actions/fleet/control/enrollment", headers={"Authorization": "Bearer read-only"}
        )
        metadata = await client.get(
            "/actions/fleet/control", headers={"Authorization": "Bearer read-only"}
        )
    assert response.status_code == 401
    assert metadata.status_code == 200
    assert controller.calls == []
