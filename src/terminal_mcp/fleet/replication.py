from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import timedelta

import httpx

from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.identity import (
    AgentIdentityRecord,
    SignedAgentIdentity,
    sign_identity_record,
    verify_identity_record,
)


class FleetReplicationService:
    def __init__(
        self,
        config: FleetConfig,
        store,
        agent_store,
        *,
        max_session_seconds: float,
        client_factory=None,
        events=None,
    ):
        self.config = config
        self.store = store
        self.agent_store = agent_store
        self.max_session_seconds = float(max_session_seconds)
        self.client_factory = client_factory or self._default_client
        self.events = events
        self._task: asyncio.Task | None = None
        self._kick_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def _default_client(self):
        return httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

    def _emit(self, event: str, *, outcome: str, **fields) -> None:
        if self.events is not None:
            self.events.emit(event, outcome=outcome, **fields)

    def authenticate(self, peer_instance_id: str, authorization: str) -> FleetPeer | None:
        peer = self.config.peers_by_id.get(peer_instance_id)
        prefix = "bearer "
        if peer is None or not authorization.lower().startswith(prefix):
            return None
        token = authorization[len(prefix) :].strip()
        if not token or not secrets.compare_digest(token, peer.auth_token):
            return None
        return peer

    async def receive(
        self,
        envelope: SignedAgentIdentity,
        *,
        authenticated_peer_id: str | None = None,
    ) -> str:
        record = envelope.record
        if record.source_instance_id == self.config.instance_id:
            raise ValueError("remote peer cannot author local instance identity")
        peer = self.config.peers_by_id.get(record.source_instance_id)
        if peer is None:
            raise ValueError("identity source is not a configured fleet peer")
        if authenticated_peer_id is not None and authenticated_peer_id != record.source_instance_id:
            raise ValueError("authenticated peer does not match identity source")
        if not verify_identity_record(record, envelope.signature, peer.public_key):
            raise ValueError("identity signature is invalid")
        status = await self.store.apply(envelope)
        if status == "conflict":
            raise ValueError("identity revision conflicts with stored record")
        return status

    async def local_envelope(self, agent_id: str) -> SignedAgentIdentity | None:
        return await self.store.get(self.config.instance_id, agent_id)

    @staticmethod
    def _same_lifecycle(
        previous: AgentIdentityRecord,
        *,
        state: str,
        session_started_at: str,
        expires_at: str,
        ended_at: str | None,
        end_reason: str | None,
    ) -> bool:
        return (
            previous.state == state
            and previous.session_started_at == session_started_at
            and previous.expires_at == expires_at
            and previous.ended_at == ended_at
            and previous.end_reason == end_reason
        )

    async def sync_local_session(self, agent_id: str) -> SignedAgentIdentity | None:
        session = await self.agent_store.get_session(agent_id)
        if session is None:
            return None
        session_started_at = session["registered_at"]
        expires_at = utc_text(
            parse_utc(session_started_at) + timedelta(seconds=self.max_session_seconds)
        )
        previous = await self.local_envelope(agent_id)
        if previous and self._same_lifecycle(
            previous.record,
            state=session["state"],
            session_started_at=session_started_at,
            expires_at=expires_at,
            ended_at=session["ended_at"],
            end_reason=session["end_reason"],
        ):
            return previous

        revision = previous.record.revision + 1 if previous else 1
        updated_at = session["ended_at"] or session_started_at
        record = AgentIdentityRecord(
            source_instance_id=self.config.instance_id,
            agent_id=agent_id,
            state=session["state"],
            session_started_at=session_started_at,
            expires_at=expires_at,
            updated_at=updated_at,
            revision=revision,
            ended_at=session["ended_at"],
            end_reason=session["end_reason"],
        )
        envelope = SignedAgentIdentity(
            record,
            sign_identity_record(record, self.config.signing_private_key),
        )
        status = await self.store.apply(envelope)
        if status not in {"applied", "duplicate"}:
            raise RuntimeError(f"local fleet identity write returned {status}")
        await self.store.enqueue(
            [peer.instance_id for peer in self.config.peers],
            envelope,
        )
        self.kick()
        return envelope

    async def sync_local_sessions(self, limit: int = 1000) -> None:
        sessions = await self.agent_store.history_sessions("0001-01-01T00:00:00.000Z", limit=limit)
        for session in sessions:
            await self.sync_local_session(session["agent_id"])

    def schedule_local_sync(self, agent_id: str) -> None:
        async def run():
            try:
                await self.sync_local_session(agent_id)
            except Exception as exc:
                self._emit("fleet_identity_sync", outcome="error", error=exc.__class__.__name__)

        asyncio.create_task(run())

    def kick(self) -> None:
        if self._task is None:
            return
        if self._kick_task is not None and not self._kick_task.done():
            return

        async def run():
            try:
                await self.flush_once()
            except Exception as exc:
                self._emit("fleet_replication_flush", outcome="error", error=exc.__class__.__name__)

        self._kick_task = asyncio.create_task(run())

    def _headers(self, peer: FleetPeer) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {peer.auth_token}",
            "X-Terminal-MCP-Peer": self.config.instance_id,
        }

    async def flush_once(self) -> None:
        if not self.config.peers:
            return
        async with self.client_factory() as client:
            for peer in self.config.peers:
                for envelope in await self.store.pending(peer.instance_id):
                    try:
                        response = await client.post(
                            f"{peer.origin}/internal/fleet/identities",
                            headers=self._headers(peer),
                            json=envelope.as_dict(),
                        )
                        response.raise_for_status()
                    except Exception as exc:
                        await self.store.delivery_result(
                            peer.instance_id,
                            envelope,
                            error=exc.__class__.__name__,
                        )
                        self._emit(
                            "fleet_replication_delivery",
                            outcome="error",
                            peer_instance_id=peer.instance_id,
                        )
                        break
                    await self.store.delivery_result(peer.instance_id, envelope, error=None)
                    self._emit(
                        "fleet_replication_delivery",
                        outcome="success",
                        peer_instance_id=peer.instance_id,
                    )

    async def pull_once(self) -> None:
        if not self.config.peers:
            return
        async with self.client_factory() as client:
            for peer in self.config.peers:
                try:
                    response = await client.get(
                        f"{peer.origin}/internal/fleet/identities",
                        headers=self._headers(peer),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    identities = payload.get("identities", [])
                    if not isinstance(identities, list):
                        raise ValueError("peer identities response must be a list")
                    for item in identities:
                        envelope = SignedAgentIdentity.from_dict(item)
                        if envelope.record.source_instance_id == self.config.instance_id:
                            continue
                        await self.receive(envelope)
                except Exception as exc:
                    self._emit(
                        "fleet_replication_pull",
                        outcome="error",
                        peer_instance_id=peer.instance_id,
                        error=exc.__class__.__name__,
                    )
                    continue
                self._emit(
                    "fleet_replication_pull",
                    outcome="success",
                    peer_instance_id=peer.instance_id,
                )

    async def list_identities(self, *, agent_id: str | None = None, limit: int = 500) -> list[dict]:
        return [item.as_dict() for item in await self.store.list(agent_id=agent_id, limit=limit)]

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        await self.sync_local_sessions()

        async def loop():
            while not self._stopped.is_set():
                try:
                    await self.flush_once()
                    await self.pull_once()
                    await self.sync_local_sessions()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._emit(
                        "fleet_replication_cycle",
                        outcome="error",
                        error=exc.__class__.__name__,
                    )
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=self.config.replication_interval_seconds,
                    )
                except TimeoutError:
                    pass

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        self._stopped.set()
        for task in (self._task, self._kick_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._kick_task = None
