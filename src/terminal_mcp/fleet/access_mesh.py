"""Authenticated, bounded event delivery for autonomous access replicas.

Peer failures affect replication health only. Public writes never call this path.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent

_LOG = logging.getLogger(__name__)
MAX_WIRE_BYTES = 64 * 1024


class AccessMeshReplication:
    def __init__(self, mesh, config, *, client=None):
        self.mesh = mesh
        self.store = mesh.store
        self.config = config
        self.client = client
        self._owned_client = False
        self._stop = asyncio.Event()
        self._task = None
        self._snapshot_after = {peer.instance_id: "" for peer in config.peers}
        self._last_snapshot_pass = {peer.instance_id: 0.0 for peer in config.peers}
        self.peer_health = {
            peer.instance_id: {"status": "degraded", "reason": "catchup_pending"}
            for peer in self.peers
        }

    @property
    def peers(self):
        return tuple(
            peer for peer in self.config.peers if peer.instance_id in self.store.trusted_issuers
        )

    async def request(self, peer, path: str, payload: dict) -> dict:
        if peer.instance_id not in self.store.trusted_issuers:
            raise AccessMeshError("access_mesh_untrusted_peer")
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=min(3.0, self.config.request_timeout_seconds))
            self._owned_client = True
        async with self.client.stream(
            "POST",
            f"{peer.origin}/internal/fleet/access-mesh/{path}",
            json=payload,
            headers={
                "x-terminal-mcp-peer": self.config.instance_id,
                "authorization": f"Bearer {self.config.outbound_auth_token(peer)}",
            },
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_WIRE_BYTES:
                    raise AccessMeshError("access_mesh_invalid_wire_event")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise AccessMeshError("access_mesh_invalid_wire_event")
            return result

    async def _snapshot(self, peer) -> None:
        result = await self.request(
            peer, "snapshot", {"after": self._snapshot_after[peer.instance_id], "limit": 20}
        )
        rows = result.get("slots")
        if result.get("ok") is not True or not isinstance(rows, list) or len(rows) > 20:
            raise AccessMeshError("access_mesh_invalid_snapshot")
        previous = self._snapshot_after[peer.instance_id]
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("slot_id"), str)
                or row["slot_id"] <= previous
            ):
                raise AccessMeshError("access_mesh_invalid_snapshot")
            await asyncio.to_thread(
                self.store.apply_snapshot, row, authenticated_peer_id=peer.instance_id
            )
            previous = row["slot_id"]
        self._snapshot_after[peer.instance_id] = previous if len(rows) == 20 else ""
        if len(rows) < 20:
            self._last_snapshot_pass[peer.instance_id] = asyncio.get_running_loop().time()

    async def _deliver(self, peer) -> None:
        pending = await asyncio.to_thread(
            self.store.pending_outbox, peer_node_id=peer.instance_id, limit=20
        )
        if not pending:
            return False
        payload = {
            "events": [
                {"event": item.event.to_wire(), "issued_kind": item.issued_kind} for item in pending
            ]
        }
        if len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_WIRE_BYTES:
            raise AccessMeshError("access_mesh_invalid_wire_event")
        result = await self.request(peer, "events", payload)
        acked = result.get("acked", [])
        if not isinstance(acked, list) or any(not isinstance(value, str) for value in acked):
            raise AccessMeshError("access_mesh_invalid_wire_event")
        allowed = {item.event.event_id for item in pending}
        if not set(acked).issubset(allowed):
            raise AccessMeshError("access_mesh_invalid_wire_event")
        for event_id in acked:
            await asyncio.to_thread(
                self.store.acknowledge_delivery,
                peer_node_id=peer.instance_id,
                event_id=event_id,
                authenticated_peer_id=peer.instance_id,
            )
        # A lagging consumer asks for one issuer-authoritative snapshot. The next
        # event pass then receives stale/duplicate acknowledgements normally.
        gap = result.get("snapshot_slot_id")
        if gap is not None:
            if gap not in {item.event.slot_id for item in pending}:
                raise AccessMeshError("access_mesh_invalid_snapshot")
            snapshots = await asyncio.to_thread(
                self.store.snapshot_page, issuer_id=self.store.local_node_id, limit=50
            )
            snapshot = next((row for row in snapshots if row["slot_id"] == gap), None)
            if snapshot is None:
                # Use an exact lookup rather than silently accepting an incomplete page.
                with self.store._connect() as db:
                    row = db.execute(
                        "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                        (self.store.local_node_id, gap),
                    ).fetchone()
                    snapshot = dict(row) if row else None
            if snapshot is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            restored = await self.request(peer, "snapshot/apply", {"slot": snapshot})
            if restored.get("ok") is not True:
                raise AccessMeshError("access_mesh_invalid_snapshot")
        elif result.get("ok") is not True:
            raise AccessMeshError(str(result.get("code") or "access_mesh_delivery_failed"))
        return True

    async def sync_peer(self, peer) -> None:
        try:
            # Periodic paginated snapshots also repair lost ACKs, a restored replica,
            # or peers added after the original issuance/outbox recipient set.
            now = asyncio.get_running_loop().time()
            checked = False
            if (
                self._snapshot_after[peer.instance_id]
                or now - self._last_snapshot_pass[peer.instance_id] >= 30
            ):
                await self._snapshot(peer)
                checked = True
            delivered = await self._deliver(peer)
            if checked or delivered:
                self.peer_health[peer.instance_id] = {"status": "healthy"}
        except Exception as exc:
            self.peer_health[peer.instance_id] = {
                "status": "degraded",
                "reason": getattr(exc, "code", type(exc).__name__),
            }

    async def tick(self) -> None:
        await asyncio.gather(*(self.sync_peer(peer) for peer in self.peers))

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="access-mesh-event-delivery")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
        if self._owned_client:
            await self.client.aclose()
            self.client = None
            self._owned_client = False

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass


def build_access_mesh_router(mesh, replication_auth) -> APIRouter:
    router = APIRouter()

    def authenticated(peer_id: str, authorization: str) -> str:
        peer = replication_auth.authenticate(peer_id, authorization)
        if peer is None or peer.instance_id not in mesh.store.trusted_issuers:
            raise HTTPException(401, "invalid access mesh peer")
        return peer.instance_id

    def bounded(payload: dict) -> None:
        if len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_WIRE_BYTES:
            raise HTTPException(413, "access mesh payload exceeds byte budget")

    @router.post("/internal/fleet/access-mesh/events", include_in_schema=False)
    async def events(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer_id = authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        items = payload.get("events")
        if set(payload) != {"events"} or not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise HTTPException(400, "invalid event batch")
        acked = []
        for item in items:
            try:
                if not isinstance(item, dict) or set(item) != {"event", "issued_kind"}:
                    raise AccessMeshError("access_mesh_invalid_wire_event")
                event = AccessSlotEvent.from_wire(item["event"])
                await asyncio.to_thread(
                    mesh.store.apply_event,
                    event,
                    authenticated_peer_id=peer_id,
                    issued_kind=item["issued_kind"],
                )
                acked.append(event.event_id)
            except AccessMeshError as exc:
                gap = (
                    item.get("event", {}).get("slot_id")
                    if isinstance(item, dict) and isinstance(item.get("event"), dict)
                    else None
                )
                return {
                    "ok": False,
                    "code": exc.code,
                    "acked": acked,
                    **(
                        {"snapshot_slot_id": gap}
                        if exc.code in {"access_mesh_unknown_slot", "access_mesh_event_gap"}
                        else {}
                    ),
                }
        return {"ok": True, "acked": acked}

    @router.post("/internal/fleet/access-mesh/snapshot", include_in_schema=False)
    async def snapshot(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) - {"after", "limit"} or not isinstance(payload.get("after", ""), str):
            raise HTTPException(400, "invalid snapshot request")
        try:
            rows = await asyncio.to_thread(
                mesh.store.snapshot_page,
                issuer_id=mesh.store.local_node_id,
                after=payload.get("after", ""),
                limit=payload.get("limit", 20),
            )
        except AccessMeshError as exc:
            raise HTTPException(400, exc.code) from exc
        result = {"ok": True, "slots": rows}
        bounded(result)
        return result

    @router.post("/internal/fleet/access-mesh/snapshot/apply", include_in_schema=False)
    async def snapshot_apply(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer_id = authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) != {"slot"}:
            raise HTTPException(400, "invalid snapshot")
        try:
            outcome = await asyncio.to_thread(
                mesh.store.apply_snapshot, payload["slot"], authenticated_peer_id=peer_id
            )
        except AccessMeshError as exc:
            return {"ok": False, "code": exc.code}
        return {"ok": True, "outcome": outcome}

    return router
