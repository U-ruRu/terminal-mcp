"""Authenticated, bounded event delivery for autonomous access replicas.

Peer failures affect replication health only. Public writes never call this path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from functools import partial

import httpx
from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent

_LOG = logging.getLogger(__name__)
MAX_WIRE_BYTES = 64 * 1024


class PinnedAccessMeshPeerAuth:
    """Inbound peer trust pinned to the immutable Access Mesh bootstrap config.

    Managed Fleet Control may rotate FleetReplicationService.config at runtime;
    Access Mesh uses its own static peer proof/token pair in outbound requests.
    """

    def __init__(self, config):
        self.config = config

    def authenticate(self, peer_instance_id: str, authorization: str):
        peer = self.config.peers_by_id.get(peer_instance_id)
        prefix = "bearer "
        if peer is None or not authorization.lower().startswith(prefix):
            return None
        token = authorization[len(prefix) :].strip()
        if not token or not secrets.compare_digest(token, peer.auth_token):
            return None
        return peer


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

    @staticmethod
    def _consume_abandoned(task: asyncio.Task) -> None:
        """Drain a late transport result without delaying public admission."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOG.debug("Late Mesh peer reply ignored after budget", exc_info=True)

    def _record_late_reservation_reply(
        self, task: asyncio.Task, *, number: str, attempt_id: str, peer_id: str
    ) -> None:
        """Persist a late conflict without letting it change the issued number."""
        try:
            response = task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            _LOG.debug("Mesh peer timed out without late reply", exc_info=True)
            return
        if not isinstance(response, dict) or response.get("code") != "number_conflict":
            return
        try:
            self.store.numbers.record_late_conflict(
                number=number,
                attempt_id=attempt_id,
                peer_id=peer_id,
                suggested_number=response.get("suggested_number"),
            )
        except Exception:
            _LOG.exception("Unable to persist delayed number conflict")

    @classmethod
    async def _hard_bounded_fanout(cls, requests, *, budget: float) -> None:
        """Never await cancellation acknowledgements past the shared deadline."""
        if budget <= 0:
            return
        tasks = [asyncio.create_task(request) for request in requests]
        done, pending = await asyncio.wait(tasks, timeout=budget)
        for task in done:
            cls._consume_abandoned(task)
        for task in pending:
            task.add_done_callback(cls._consume_abandoned)
            task.cancel()

    async def negotiate_number(
        self, *, preferred: str | None = None, _budget_seconds: float = 30.0
    ):
        """Choose a reserved number within one fixed 30-second admission budget.

        A conflict from any peer is processed immediately, even while other
        peers are unresponsive. Individual retries never reset the deadline.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + min(30.0, max(0.0, _budget_seconds))
        # Reserve at most the last three seconds for commit/outbox fanout.
        negotiation_end = max(loop.time(), end - min(3.0, _budget_seconds * 0.1))
        attempt_id = "nr_" + secrets.token_urlsafe(16)
        number = preferred or f"{secrets.randbelow(10000):04d}"
        while True:
            local = await asyncio.to_thread(
                self.store.numbers.reserve, number=number, attempt_id=attempt_id
            )
            if not local["ok"]:
                number = local["suggested_number"]
                if loop.time() < negotiation_end:
                    continue
                # The suggested number is itself reserved by the local store.
                return number, attempt_id, end
            if not self.peers or loop.time() >= negotiation_end:
                return number, attempt_id, end

            tasks = {
                asyncio.create_task(
                    self.request(
                        peer, "numbers/reserve", {"number": number, "attempt_id": attempt_id}
                    )
                ): peer.instance_id
                for peer in self.peers
            }
            pending = set(tasks)
            suggested = None
            confirmed = 0
            try:
                while pending and loop.time() < negotiation_end:
                    done, pending = await asyncio.wait(
                        pending,
                        timeout=max(0, negotiation_end - loop.time()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        break
                    for task in done:
                        try:
                            response = task.result()
                        except (asyncio.CancelledError, Exception):
                            continue
                        if (
                            isinstance(response, dict)
                            and response.get("code") == "number_conflict"
                            and isinstance(response.get("suggested_number"), str)
                        ):
                            suggested = response["suggested_number"]
                            break
                        if isinstance(response, dict) and response.get("ok") is True:
                            confirmed += 1
                    if suggested is not None:
                        break
            finally:
                # Cancellation of remote transports is not guaranteed to be
                # prompt. Never await them after the fixed negotiation cap.
                for task, peer_id in tasks.items():
                    if not task.done():
                        task.add_done_callback(
                            partial(
                                self._record_late_reservation_reply,
                                number=number,
                                attempt_id=attempt_id,
                                peer_id=peer_id,
                            )
                        )
                        task.cancel()

            if suggested is not None and loop.time() < negotiation_end:
                number = suggested
                continue
            if confirmed == len(tasks) or not pending:
                # Every peer responded: success completes early only if all
                # accepted. Failed calls consume the remaining bounded window.
                if confirmed == len(tasks):
                    return number, attempt_id, end
            if loop.time() < negotiation_end:
                await asyncio.sleep(max(0, negotiation_end - loop.time()))
            return number, attempt_id, end

    async def announce_number(
        self,
        *,
        number,
        issuer_id,
        slot_id,
        logical_agent_id,
        started_at,
        hard_expires_at,
        attempt_id,
        deadline,
    ):
        """Deliver the identity *and the actual SlotIssued event* before return.

        Publishing a claim alone is insufficient for immediate attach on peers.
        Any failed delivery stays in the existing durable event outbox.
        """
        payload = dict(
            number=number,
            issuer_id=issuer_id,
            slot_id=slot_id,
            logical_agent_id=logical_agent_id,
            started_at=started_at,
            hard_expires_at=hard_expires_at,
            attempt_id=attempt_id,
        )
        budget = deadline - asyncio.get_running_loop().time()
        if budget <= 0 or not self.peers:
            return

        async def publish(peer):
            try:
                await self.request(peer, "numbers/commit", payload)
            finally:
                # Release alternative peer reservations even when commit
                # succeeds only partially. The selected number is already
                # committed as a durable claim on successful peers.
                try:
                    await self.request(peer, "numbers/release", {"attempt_id": attempt_id})
                finally:
                    await self._deliver(peer)

        await self._hard_bounded_fanout(
            (publish(peer) for peer in self.peers),
            budget=budget,
        )

    async def announce_start(self, **event):
        """Propagate an explicit Access.start, including its actual timestamp."""
        if not self.peers:
            return

        async def publish(peer):
            try:
                await self.request(peer, "numbers/start", event)
            finally:
                await self._deliver(peer)

        await self._hard_bounded_fanout(
            (publish(peer) for peer in self.peers),
            budget=2.5,
        )

    async def sync_starts(self, peer):
        cursor = ""
        while True:
            response = await self.request(peer, "numbers/starts", {"after": cursor, "limit": 25})
            if response.get("ok") is not True or not isinstance(response.get("starts"), list):
                raise AccessMeshError("invalid_session_start_snapshot")
            for item in response["starts"]:
                await asyncio.to_thread(self.store.numbers.record_start, **item)
            cursor = response.get("next_cursor")
            if not cursor:
                return

    async def announce_end(self, **event):
        # The durable local event remains authoritative when peers are offline.
        if not self.peers:
            return
        await self._hard_bounded_fanout(
            (self.request(peer, "numbers/end", event) for peer in self.peers),
            budget=2.5,
        )

    async def sync_ends(self, peer):
        cursor = ""
        while True:
            response = await self.request(peer, "numbers/ends", {"after": cursor, "limit": 25})
            if response.get("ok") is not True or not isinstance(response.get("ends"), list):
                raise AccessMeshError("invalid_session_end_snapshot")
            for event in response["ends"]:
                await asyncio.to_thread(self.store.numbers.record_end, **event)
            cursor = response.get("next_cursor")
            if not cursor:
                return

    async def sync_numbers(self, peer):
        cursor = ""
        while True:
            result = await self.request(peer, "numbers/snapshot", {"after": cursor, "limit": 25})
            if result.get("ok") is not True or not isinstance(result.get("claims"), list):
                raise AccessMeshError("invalid_session_snapshot")
            if isinstance(result.get("reservations"), list):
                await asyncio.to_thread(
                    self.store.numbers.merge_reservations, result["reservations"]
                )
            for claim in result["claims"]:
                await asyncio.to_thread(self.store.numbers.register, **claim)
            cursor = result.get("next_cursor")
            if not cursor:
                return

    async def sync_incidents(self, peer):
        """Anti-entropy for late reservation and true identity collisions."""
        cursor = ""
        while True:
            response = await self.request(
                peer,
                "numbers/incidents",
                {"after": cursor, "limit": 25},
            )
            if response.get("ok") is not True or not isinstance(response.get("incidents"), list):
                raise AccessMeshError("invalid_session_snapshot")
            await asyncio.to_thread(self.store.numbers.merge_incidents, response["incidents"])
            following = response.get("next_cursor")
            if not following:
                return
            if not isinstance(following, str) or following == cursor:
                raise AccessMeshError("invalid_session_snapshot")
            cursor = following

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
                await self.sync_numbers(peer)
                await self.sync_starts(peer)
                await self.sync_ends(peer)
                await self.sync_incidents(peer)
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

    @router.post("/internal/fleet/access-mesh/numbers/reserve", include_in_schema=False)
    async def number_reserve(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) != {"number", "attempt_id"}:
            raise HTTPException(400, "invalid reservation")
        try:
            return await asyncio.to_thread(mesh.store.numbers.reserve, **payload)
        except AccessMeshError as exc:
            return {"ok": False, "code": exc.code}

    @router.post("/internal/fleet/access-mesh/numbers/release", include_in_schema=False)
    async def number_release(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) != {"attempt_id"}:
            raise HTTPException(400, "invalid reservation release")
        attempt_id = payload["attempt_id"]
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.startswith("nr_")
            or len(attempt_id) > 128
        ):
            raise HTTPException(400, "invalid reservation release")
        await asyncio.to_thread(mesh.store.numbers.release, attempt_id)
        return {"ok": True}

    @router.post("/internal/fleet/access-mesh/numbers/commit", include_in_schema=False)
    async def number_commit(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer_id = authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if payload.get("issuer_id") != peer_id:
            raise HTTPException(400, "issuer does not match authenticated peer")
        try:
            return await asyncio.to_thread(mesh.store.numbers.register, **payload)
        except (AccessMeshError, TypeError, ValueError) as exc:
            return {"ok": False, "code": getattr(exc, "code", "invalid_session_registration")}

    @router.post("/internal/fleet/access-mesh/numbers/snapshot", include_in_schema=False)
    async def number_snapshot(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) - {"after", "limit"}:
            raise HTTPException(400, "invalid number snapshot")
        after, limit = payload.get("after", ""), payload.get("limit", 25)
        if (
            not isinstance(after, str)
            or len(after) > 300
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise HTTPException(400, "invalid number snapshot cursor")
        rows = await asyncio.to_thread(mesh.store.numbers.snapshot)
        rows = [r for r in rows if (r["issuer_id"] + ":" + r["slot_id"]) > after][:limit]
        reservations = await asyncio.to_thread(mesh.store.numbers.reservations)
        return {
            "ok": True,
            "claims": rows,
            "reservations": reservations[:50],
            "next_cursor": (rows[-1]["issuer_id"] + ":" + rows[-1]["slot_id"])
            if len(rows) == limit
            else None,
        }

    @router.post("/internal/fleet/access-mesh/numbers/start", include_in_schema=False)
    async def number_start(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) != {
            "number",
            "issuer_id",
            "slot_id",
            "event_id",
            "active_from",
            "started_at",
            "hard_expires_at",
        }:
            raise HTTPException(400, "invalid number start")
        if payload["issuer_id"] != peer:
            raise HTTPException(400, "issuer does not match authenticated peer")
        try:
            return await asyncio.to_thread(mesh.store.numbers.record_start, **payload)
        except (AccessMeshError, ValueError, TypeError) as exc:
            return {"ok": False, "code": getattr(exc, "code", "invalid_session_start")}

    @router.post("/internal/fleet/access-mesh/numbers/incidents", include_in_schema=False)
    async def number_incidents(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) - {"after", "limit"}:
            raise HTTPException(400, "invalid incident snapshot")
        after = payload.get("after", "")
        limit = payload.get("limit", 25)
        if type(limit) is not int or not 1 <= limit <= 25:
            raise HTTPException(400, "invalid incident page size")
        if not isinstance(after, str) or len(after) > 1000:
            raise HTTPException(400, "invalid incident cursor")
        try:
            after_key = tuple(json.loads(after)) if after else None
            rows = await asyncio.to_thread(
                mesh.store.numbers.incident_page, after=after_key, limit=limit
            )
        except (AccessMeshError, ValueError, TypeError) as exc:
            raise HTTPException(400, "invalid incident cursor") from exc
        cursor = (
            json.dumps(
                [rows[-1][key] for key in ("number", "issuer_id", "slot_id", "collided_with")],
                separators=(",", ":"),
            )
            if len(rows) == limit
            else None
        )
        return {"ok": True, "incidents": rows, "next_cursor": cursor}

    @router.post("/internal/fleet/access-mesh/numbers/starts", include_in_schema=False)
    async def number_starts(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) - {"after", "limit"}:
            raise HTTPException(400, "invalid start snapshot")
        after, limit = payload.get("after", ""), payload.get("limit", 25)
        if (
            not isinstance(after, str)
            or len(after) > 550
            or type(limit) is not int
            or not 1 <= limit <= 25
        ):
            raise HTTPException(400, "invalid start snapshot cursor")
        rows = await asyncio.to_thread(mesh.store.numbers.start_snapshot)
        rows = [
            r for r in rows if (r["issuer_id"] + ":" + r["slot_id"] + ":" + r["event_id"]) > after
        ][:limit]
        return {
            "ok": True,
            "starts": rows,
            "next_cursor": rows[-1]["issuer_id"]
            + ":"
            + rows[-1]["slot_id"]
            + ":"
            + rows[-1]["event_id"]
            if len(rows) == limit
            else None,
        }

    @router.post("/internal/fleet/access-mesh/numbers/end", include_in_schema=False)
    async def number_end(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) != {"number", "cycle_key", "issuer_id", "slot_id", "event_id", "ended_at"}:
            raise HTTPException(400, "invalid number end")
        if payload["issuer_id"] != peer:
            raise HTTPException(400, "issuer does not match authenticated peer")
        try:
            return await asyncio.to_thread(mesh.store.numbers.record_end, **payload)
        except (AccessMeshError, ValueError, TypeError) as exc:
            return {"ok": False, "code": getattr(exc, "code", "invalid_session_end")}

    @router.post("/internal/fleet/access-mesh/numbers/ends", include_in_schema=False)
    async def number_ends(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticated(x_terminal_mcp_peer, authorization)
        bounded(payload)
        if set(payload) - {"after", "limit"}:
            raise HTTPException(400, "invalid number end snapshot")
        after, limit = payload.get("after", ""), payload.get("limit", 25)
        if (
            not isinstance(after, str)
            or len(after) > 550
            or type(limit) is not int
            or not 1 <= limit <= 25
        ):
            raise HTTPException(400, "invalid number end snapshot cursor")
        events = await asyncio.to_thread(mesh.store.numbers.end_snapshot)
        events = [
            e for e in events if e["number"] + ":" + e["cycle_key"] + ":" + e["event_id"] > after
        ][:limit]
        return {
            "ok": True,
            "ends": events,
            "next_cursor": events[-1]["number"]
            + ":"
            + events[-1]["cycle_key"]
            + ":"
            + events[-1]["event_id"]
            if len(events) == limit
            else None,
        }

    return router
