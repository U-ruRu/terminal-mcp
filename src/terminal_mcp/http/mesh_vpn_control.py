"""Authenticated Console control for a local server's Mesh WireGuard transport.

The mobile application already controls Fleet membership. This router adds
per-node tunnel enrollment/health and deliberate transport cutover on that
same operator permission boundary. No peer can bootstrap Fleet trust here.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.persistent_admission import current_admission_context
from terminal_mcp.mesh_vpn import (
    VPNConnectivityError,
    VPNError,
    _load,
    create_offer,
    init,
    install_units,
    join,
    revoke_peer,
    set_backend,
    status,
    switch,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepareRequest(StrictRequest):
    backend: str = Field(default="auto", pattern="^(auto|kernel|userspace)$")
    interface: str = Field(default="tmcpwg", pattern=r"^[A-Za-z0-9_-]{1,15}$")
    overlay_ip: str = Field(max_length=50)
    endpoint: str = Field(max_length=100)
    listen_port: int = Field(default=53148, ge=1, le=65535)
    proxy_port: int = Field(default=18080, ge=1024, le=65535)


class JoinRequest(StrictRequest):
    peer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    offer: dict


class PeerRequest(StrictRequest):
    peer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SwitchRequest(PeerRequest):
    mode: str = Field(pattern="^(https|wireguard)$")
    expected_topology_revision: int | None = Field(default=None, ge=0)


class BackendRequest(StrictRequest):
    backend: str = Field(pattern="^(auto|kernel|userspace)$")


def build_mesh_vpn_control_router(controller, application, settings) -> APIRouter:
    router = APIRouter(tags=["fleet-control"])
    state_dir = Path(settings.mesh_vpn_state_dir)
    transport_file = Path(settings.fleet_peer_transports_path)

    async def authorized() -> dict:
        actor = ActorContext.from_admission(
            current_admission_context(),
            transport="http",
            endpoint_role="operator",
            node_id=controller.config.instance_id,
        )
        try:
            result = await application.state(actor)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="operator_required") from exc
        if not result.get("ok"):
            raise HTTPException(status_code=403, detail="fleet_control_unavailable")
        return result["control"]

    def peer_trust(control: dict, peer_id: str) -> str:
        if not control.get("managed"):
            raise VPNError("managed Mesh membership is required")
        nodes = {node["node_id"]: node for node in control.get("nodes", [])}
        local = nodes.get(controller.config.instance_id)
        peer = nodes.get(peer_id)
        if not local or not peer or peer.get("state") == "detached":
            raise VPNError("peer must be an active member of the managed Mesh")
        if (
            not local.get("mesh_id")
            or local["mesh_id"] != peer.get("mesh_id")
            or peer_id == controller.config.instance_id
        ):
            raise VPNError("peer must belong to the same managed Mesh")
        public_key = peer.get("public_key")
        if not isinstance(public_key, str) or not public_key:
            raise VPNError("peer trust public key is missing")
        return public_key

    def snapshot(control: dict) -> dict:
        local_file = state_dir / "local.json"
        prepared = local_file.is_file()
        observed = status(state_dir) if prepared else None
        overrides = _load(transport_file) if transport_file.exists() else {}
        known = (
            []
            if not prepared
            else [
                p["instance_id"]
                for p in (_load(f) for f in sorted((state_dir / "peers").glob("*.json")))
            ]
        )
        return {
            "ok": True,
            "node_id": controller.config.instance_id,
            "prepared": prepared,
            "tunnel": observed,
            "overlay_ip": _load(local_file)["overlay_ip"] if prepared else None,
            "endpoint": _load(local_file)["endpoint"] if prepared else None,
            "backend": _load(local_file)["backend"] if prepared else None,
            "peers": [
                {
                    "node_id": peer_id,
                    "enrolled": peer_id in known,
                    "mode": "wireguard" if peer_id in overrides else "https",
                }
                for peer_id in sorted(
                    set(known)
                    | {
                        node.get("node_id")
                        for node in control.get("nodes", [])
                        if node.get("node_id") != controller.config.instance_id
                        and isinstance(node.get("node_id"), str)
                    }
                )
            ],
            "offer": (
                create_offer(_load(local_file), controller.config.signing_private_key)
                if prepared
                else None
            ),
        }

    def response(call, *, conflict: bool = False):
        try:
            return call()
        except VPNConnectivityError as exc:
            return {"ok": False, "code": "vpn_unavailable", "error": str(exc)}
        except VPNError as exc:
            return {
                "ok": False,
                "code": "vpn_conflict" if conflict else "vpn_invalid",
                "error": str(exc),
            }
        except (OSError, ValueError, subprocess.SubprocessError, httpx.RequestError) as exc:
            return {"ok": False, "code": "vpn_unavailable", "error": type(exc).__name__}

    @router.get("/actions/fleet/control/transport", operation_id="getManagedFleetVpn")
    async def read():
        control = await authorized()
        return response(lambda: snapshot(control))

    @router.post("/actions/fleet/control/transport/prepare", operation_id="prepareManagedFleetVpn")
    async def prepare(body: PrepareRequest):
        control = await authorized()
        if not control.get("managed"):
            return {"ok": False, "code": "mesh_unmanaged", "error": "Join a managed Mesh first"}

        def action():
            params = body.model_dump()
            return {
                "ok": True,
                "local": init(state_dir, instance_id=controller.config.instance_id, **params),
            }

        return await asyncio.to_thread(response, action)

    @router.post("/actions/fleet/control/transport/enroll", operation_id="enrollManagedFleetVpn")
    async def enroll(body: JoinRequest):
        control = await authorized()
        result = response(lambda: peer_trust(control, body.peer_id))
        if isinstance(result, dict) and result.get("ok") is False:
            return result
        pinned_key = result
        return await asyncio.to_thread(
            response,
            lambda: {
                "ok": True,
                "peer_id": join(
                    state_dir,
                    body.offer,
                    peer_id=body.peer_id,
                    pinned_key=pinned_key,
                    approved_fingerprint=None,
                )["instance_id"],
            },
        )

    @router.post(
        "/actions/fleet/control/transport/activate", operation_id="activateManagedFleetVpn"
    )
    async def activate():
        control = await authorized()
        if not control.get("managed"):
            return {"ok": False, "code": "mesh_unmanaged", "error": "Managed Mesh required"}

        def action():
            # Fixed OS operations. Never accept commands, unit paths or shell from HTTP.
            units = install_units(state_dir, Path("/etc/systemd/system"))
            for command in (
                ("systemctl", "daemon-reload"),
                ("systemctl", "enable", "--now", *units),
            ):
                completed = subprocess.run(command, capture_output=True, timeout=30)
                if completed.returncode:
                    raise VPNError("VPN service activation failed")
            return {"ok": True, "running": status(state_dir)["running"]}

        return await asyncio.to_thread(response, action)

    @router.post(
        "/actions/fleet/control/transport/backend", operation_id="setManagedFleetVpnBackend"
    )
    async def backend(body: BackendRequest):
        await authorized()
        return await asyncio.to_thread(
            response, lambda: {"ok": True, **set_backend(state_dir, body.backend)}
        )

    @router.post(
        "/actions/fleet/control/transport/switch", operation_id="switchManagedFleetVpnPeer"
    )
    async def select(body: SwitchRequest):
        control = await authorized()
        try:
            peer_trust(control, body.peer_id)
            revision = (control.get("revisions") or {}).get("topology")
            if (
                body.expected_topology_revision is not None
                and body.expected_topology_revision != revision
            ):
                raise VPNError("stale Fleet topology revision")
        except VPNError as exc:
            return {"ok": False, "code": "vpn_conflict", "error": str(exc)}

        def existing_overrides():
            return _load(transport_file) if transport_file.exists() else {}

        previous = await asyncio.to_thread(existing_overrides)
        result = await asyncio.to_thread(
            response,
            lambda: {
                "ok": True,
                **switch(state_dir, transport_file, peer_id=body.peer_id, mode=body.mode),
            },
        )
        if not result.get("ok"):
            return result
        try:
            await controller.reconcile_local()
        except Exception:
            # Keep local Fleet config consistent if runtime reconciliation fails.
            from terminal_mcp.mesh_vpn import _atomic_json

            await asyncio.to_thread(_atomic_json, transport_file, previous)
            await controller.reconcile_local()
            return {
                "ok": False,
                "code": "vpn_reconcile_failed",
                "error": "Runtime transition aborted",
            }
        result["restart_required"] = False
        return result

    @router.post(
        "/actions/fleet/control/transport/revoke", operation_id="revokeManagedFleetVpnPeer"
    )
    async def revoke(body: PeerRequest):
        await authorized()
        result = await asyncio.to_thread(
            response, lambda: {"ok": True, **revoke_peer(state_dir, body.peer_id, transport_file)}
        )
        if result.get("ok"):
            await controller.reconcile_local()
        return result

    return router
