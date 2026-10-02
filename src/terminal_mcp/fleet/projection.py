from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from terminal_mcp.fleet.projection_storage import FleetProjectionError
from terminal_mcp.fleet.source_query import CURRENT_SCOPE_VERSION

DELTA_BATCH_SIZE = 250
MAX_SYNC_PAGES = 10_000
PROJECTION_V2_CAPABILITIES = frozenset(
    {
        "fleet.projection.current-v2",
        "fleet.projection.materialized-delta-v2",
        "fleet.projection.query-relay-v2",
        "fleet.projection.scope-freshness-v2",
    }
)
_V2_SOURCE_CAPABILITIES = {
    "fleet.source.current-recovery.v2",
    "fleet.source.current-entity.v2",
}
_SCOPE_BY_ENTITY = {
    "logical_agent": "logical_agents",
    "work_session": "work_sessions",
    "node_attachment": "node_attachments",
    "attachment_presence": "attachment_presence",
    "message_obligation": "message_obligations",
    "work_claim": "work_claims",
    "task": "tasks",
    "command": "commands",
    "fleet_gate": "fleet_gates",
    "context": "contexts",
}


class FleetProjectionService:
    """Owner/follower sync and single-ingress query relay for the derived Fleet model."""

    def __init__(
        self,
        config,
        store,
        *,
        local_source=None,
        owner_node_id: str,
        client_factory=None,
        interval_seconds: float | None = None,
        metrics=None,
        events=None,
    ):
        self.config = config
        self.store = store
        self.local_source = local_source
        self.owner_node_id = owner_node_id
        self.client_factory = client_factory or self._default_client
        self.interval_seconds = float(
            interval_seconds or config.replication_interval_seconds
        )
        self.metrics = metrics
        self.events = events
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._local_source_node_id: str | None = None

    @property
    def capabilities(self) -> list[str]:
        return sorted(PROJECTION_V2_CAPABILITIES)

    def _default_client(self):
        return httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

    def _headers(self, peer) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.outbound_auth_token(peer)}",
            "X-Terminal-MCP-Peer": self.config.instance_id,
        }

    def _observe(
        self,
        stage: str,
        started: float,
        *,
        source_node_id: str,
        reason: str = "ok",
        rows: int = 0,
        size: int = 0,
    ) -> None:
        elapsed = time.monotonic() - started
        labels = (
            ("stage", stage),
            ("source_node_id", source_node_id),
            ("reason", reason),
        )
        if self.metrics:
            self.metrics.observe(
                "terminal_mcp_fleet_projection_stage_duration_seconds", elapsed, labels
            )
            self.metrics.observe("terminal_mcp_fleet_projection_rows", rows, labels)
            self.metrics.observe("terminal_mcp_fleet_projection_bytes", size, labels)
        if self.events:
            self.events.emit(
                "fleet_projection_stage",
                stage=stage,
                source_node_id=source_node_id,
                reason=reason,
                rows=int(rows),
                bytes=int(size),
                duration_ms=round(elapsed * 1000),
            )

    def _recovery_metric(self, source_node_id: str, reason: str) -> None:
        labels = (("source_node_id", source_node_id), ("reason", reason))
        if self.metrics:
            self.metrics.inc("terminal_mcp_fleet_projection_recovery_total", labels)
        if self.events:
            self.events.emit(
                "fleet_projection_recovery",
                source_node_id=source_node_id,
                reason=reason,
            )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="fleet-v1-projection")

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                continue

    async def sync_once(self) -> None:
        if self.store.role == "owner":
            await self._sync_owner()
        else:
            await self._sync_follower()

    async def _sync_owner(self) -> None:
        if self.local_source is not None:
            try:
                await self._sync_local_source()
            except Exception:
                source_node_id = self._local_source_node_id or self.config.instance_id
                current = await self.store.source_state(source_node_id)
                if current is not None:
                    await self.store.mark_source(
                        source_node_id,
                        current["source_stream_generation"],
                        "unavailable",
                    )
                    await self.store.clear_runtime_overlay(source_node_id)
                    await self._mark_problem_scopes(
                        source_node_id, "DEGRADED", "local_source_unavailable"
                    )
        if not self.config.peers:
            return
        async with self.client_factory() as client:
            for peer in self.config.peers:
                await self._sync_remote_source(client, peer)


    async def _apply_source_v1(
        self,
        manifest: dict,
        *,
        snapshot_getter,
        events_getter,
        health_getter,
    ) -> None:
        node_id = str(manifest["node_id"])
        generation = str(manifest["source_stream_generation"])
        current = await self.store.source_state(node_id)
        needs_snapshot = (
            current is None
            or current["source_stream_generation"] != generation
            or current["freshness"] == "reset_required"
        )
        if needs_snapshot:
            await self.store.apply_snapshot(await snapshot_getter())
            current = await self.store.source_state(node_id)
        high_water = int(manifest.get("high_water_source_seq") or 0)
        if current is not None and high_water > current["source_seq"]:
            page = await events_getter(
                current["source_seq"],
                current["source_stream_generation"],
                1000,
            )
            if page.get("reset_required"):
                await self.store.apply_snapshot(await snapshot_getter())
            else:
                await self.store.apply_source_page(page)
        await self.store.put_runtime_overlay(node_id, await health_getter())

    @staticmethod
    def _source_v2(manifest: dict) -> bool:
        return _V2_SOURCE_CAPABILITIES.issubset(
            {str(value) for value in manifest.get("capabilities") or []}
        )

    async def _recover_source_v2(
        self,
        manifest: dict,
        *,
        bootstrap_getter,
        current_page_getter,
        reason: str,
    ) -> tuple[list[str], int]:
        node_id = str(manifest["node_id"])
        generation = str(manifest["source_stream_generation"])
        started = time.monotonic()
        bootstrap = await bootstrap_getter()
        if (
            str(bootstrap.get("node_id") or "") != node_id
            or str(bootstrap.get("source_stream_generation") or "") != generation
        ):
            raise FleetProjectionError("source recovery identity changed")
        if int(bootstrap.get("scope_version") or 0) != CURRENT_SCOPE_VERSION:
            raise FleetProjectionError("source recovery scope version changed")
        scopes = [str(value) for value in bootstrap.get("current_scopes") or []]
        barrier = int(bootstrap.get("barrier_source_seq") or 0)
        recovery_id = f"{generation}:{barrier}:{time.monotonic_ns()}"
        self._recovery_metric(node_id, reason)
        await self.store.begin_current_recovery(
            node_id, generation, recovery_id, scopes
        )
        rows = size = 0
        for scope in scopes:
            cursor = snapshot_id = None
            pages = 0
            while True:
                page = await current_page_getter(
                    scope,
                    generation,
                    snapshot_id,
                    cursor,
                    barrier,
                )
                if page.get("reset_required"):
                    raise FleetProjectionError(
                        f"source recovery reset: {page.get('reset_reason') or 'unknown'}"
                    )
                if int(page.get("scope_version") or 0) != CURRENT_SCOPE_VERSION:
                    raise FleetProjectionError("source recovery page scope version changed")
                if int(page.get("barrier_source_seq") or -1) != barrier:
                    raise FleetProjectionError("source recovery barrier changed")
                staged = await self.store.stage_current_recovery_page(
                    node_id, recovery_id, page
                )
                rows += int(page.get("page_rows") or 0)
                size += int(page.get("page_bytes") or 0)
                pages += 1
                if staged["complete"]:
                    break
                cursor = page.get("next_cursor")
                snapshot_id = page.get("snapshot_id")
                if not cursor or pages >= MAX_SYNC_PAGES:
                    raise FleetProjectionError("source recovery pagination stalled")
        await self.store.complete_current_recovery(node_id, generation, barrier)
        self._observe(
            "recovery",
            started,
            source_node_id=node_id,
            reason=reason,
            rows=rows,
            size=size,
        )
        return scopes, barrier

    async def _materialize_page(
        self,
        page: dict,
        *,
        current_entity_getter,
    ) -> list[dict]:
        invalidations: dict[tuple[str, str], dict] = {}
        for event in page.get("events") or []:
            entity_type = str(event["entity_type"])
            entity_id = str(event["entity_id"])
            scope = _SCOPE_BY_ENTITY.get(entity_type)
            if scope:
                invalidations[(scope, entity_id)] = {"event": event, "suffix": ""}
            if entity_type == "node_attachment":
                invalidations[("attachment_presence", entity_id)] = {
                    "event": event,
                    "suffix": ":presence",
                }
            if entity_type == "message_obligation":
                logical_agent_id = str((event.get("payload") or {}).get("logical_agent_id") or "")
                if logical_agent_id:
                    invalidations[("fleet_gates", logical_agent_id)] = {
                        "event": event,
                        "suffix": ":gate",
                    }
        if not invalidations:
            return []

        semaphore = asyncio.Semaphore(32)

        async def materialize(key, value):
            scope, entity_id = key
            event = value["event"]
            async with semaphore:
                result = await current_entity_getter(
                    scope,
                    entity_id,
                    str(page["source_stream_generation"]),
                )
            if result.get("reset_required"):
                raise FleetProjectionError("source generation changed during materialization")
            entity = result.get("entity")
            if entity is None:
                return {
                    "event_id": f"{event['event_id']}{value['suffix']}",
                    "source_seq": int(event["source_seq"]),
                    "event_type": "projection.remove",
                    "entity_type": _entity_type_for_scope(scope),
                    "entity_id": entity_id,
                    "entity_revision": max(
                        1, int(event.get("entity_revision") or event["source_seq"])
                    ),
                    "payload_version": 2,
                    "payload": {},
                    "created_at": event["created_at"],
                }
            return {
                "event_id": f"{event['event_id']}{value['suffix']}",
                "source_seq": int(event["source_seq"]),
                "event_type": "projection.upsert",
                "entity_type": str(entity["entity_type"]),
                "entity_id": str(entity["entity_id"]),
                "entity_revision": int(entity["entity_revision"]),
                "payload_version": int(entity.get("payload_version") or 2),
                "payload": entity.get("payload") or {},
                "authority_node_id": entity.get("authority_node_id"),
                "authority_epoch": entity.get("authority_epoch"),
                "created_at": event["created_at"],
            }

        return list(
            await asyncio.gather(
                *(materialize(key, value) for key, value in invalidations.items())
            )
        )

    async def _apply_source_v2(
        self,
        manifest: dict,
        *,
        bootstrap_getter,
        current_page_getter,
        current_entity_getter,
        events_getter,
        health_getter,
    ) -> None:
        node_id = str(manifest["node_id"])
        generation = str(manifest["source_stream_generation"])
        current = await self.store.source_state(node_id)
        recovery_done = False
        scopes: list[str] = []
        if (
            current is None
            or current["source_stream_generation"] != generation
            or current["freshness"] == "reset_required"
        ):
            reason = (
                "bootstrap"
                if current is None
                else (
                    "generation_changed"
                    if current["source_stream_generation"] != generation
                    else "reset_required"
                )
            )
            scopes, _ = await self._recover_source_v2(
                manifest,
                bootstrap_getter=bootstrap_getter,
                current_page_getter=current_page_getter,
                reason=reason,
            )
            recovery_done = True
        elif current is not None:
            scopes = await self.store.source_scopes(node_id)

        pages = 0
        while True:
            current = await self.store.source_state(node_id)
            if current is None:
                raise FleetProjectionError("source state disappeared")
            page = await events_getter(
                current["source_seq"],
                generation,
                DELTA_BATCH_SIZE,
            )
            if page.get("reset_required"):
                if recovery_done:
                    raise FleetProjectionError("source reset repeated after bounded recovery")
                scopes, _ = await self._recover_source_v2(
                    manifest,
                    bootstrap_getter=bootstrap_getter,
                    current_page_getter=current_page_getter,
                    reason=str(page.get("reset_reason") or "event_gap"),
                )
                recovery_done = True
                continue
            started = time.monotonic()
            mutations = await self._materialize_page(
                page,
                current_entity_getter=current_entity_getter,
            )
            await self.store.apply_materialized_source_page(page, mutations)
            pages += 1
            self._observe(
                "delta",
                started,
                source_node_id=node_id,
                reason="catch_up" if pages > 1 else "steady",
                rows=len(mutations),
                size=sum(len(str(item.get("payload") or {})) for item in mutations),
            )
            cursor = int(page.get("next_cursor") or current["source_seq"])
            high_water = int(page.get("high_water_source_seq") or cursor)
            backlog = max(0, high_water - cursor)
            if self.metrics:
                labels = (("source_node_id", node_id),)
                self.metrics.set(
                    "terminal_mcp_fleet_projection_backlog_events",
                    backlog,
                    labels,
                )
                self.metrics.set(
                    "terminal_mcp_fleet_projection_source_lag_events",
                    backlog,
                    labels,
                )
            if cursor >= high_water:
                break
            if pages >= MAX_SYNC_PAGES:
                raise FleetProjectionError("source delta pagination exceeded safety bound")
        if not scopes:
            bootstrap = await bootstrap_getter()
            scopes = [str(value) for value in bootstrap.get("current_scopes") or []]
        await self.store.mark_source_live(node_id, generation, scopes)
        health = await health_getter()
        if self.metrics:
            queue_pressure = sum(
                max(0, int(item.get("queued") or 0))
                for item in health.get("queues") or []
            )
            self.metrics.set(
                "terminal_mcp_fleet_projection_queue_pressure",
                queue_pressure,
                (("source_node_id", node_id),),
            )
        await self.store.put_runtime_overlay(node_id, health)

    async def _sync_local_source(self) -> None:
        manifest = await self.local_source.manifest()
        self._local_source_node_id = str(manifest["node_id"])

        async def snapshot_getter():
            return await self.local_source.snapshot()

        async def events_getter(since, generation, limit):
            return await self.local_source.events(
                since=since,
                limit=limit,
                source_stream_generation=generation,
            )

        async def health_getter():
            return await self.local_source.runtime_health()

        if not self._source_v2(manifest):
            await self._apply_source_v1(
                manifest,
                snapshot_getter=snapshot_getter,
                events_getter=events_getter,
                health_getter=health_getter,
            )
            return

        async def bootstrap_getter():
            return await self.local_source.bootstrap()

        async def current_page_getter(scope, generation, snapshot_id, cursor, barrier):
            return await self.local_source.current_recovery(
                scope,
                source_stream_generation=generation,
                snapshot_id=snapshot_id,
                cursor=cursor,
                limit=100,
                barrier_source_seq=barrier,
            )

        async def current_entity_getter(scope, entity_id, generation):
            return await self.local_source.current_entity(
                scope,
                entity_id,
                source_stream_generation=generation,
            )

        await self._apply_source_v2(
            manifest,
            bootstrap_getter=bootstrap_getter,
            current_page_getter=current_page_getter,
            current_entity_getter=current_entity_getter,
            events_getter=events_getter,
            health_getter=health_getter,
        )

    async def _sync_remote_source(self, client, peer) -> None:
        current = await self.store.source_state(peer.instance_id)
        try:
            manifest_response = await client.get(
                f"{peer.origin}/internal/fleet/v1/source/manifest",
                headers=self._headers(peer),
            )
            manifest_response.raise_for_status()
            manifest = manifest_response.json()
            if str(manifest.get("node_id") or "") != peer.instance_id:
                raise FleetProjectionError("source peer identity mismatch")

            async def snapshot_getter():
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/snapshot",
                    headers=self._headers(peer),
                )
                response.raise_for_status()
                return response.json()

            async def events_getter(since, generation, limit):
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/events",
                    headers=self._headers(peer),
                    params={
                        "since": since,
                        "limit": limit,
                        "source_stream_generation": generation,
                    },
                )
                response.raise_for_status()
                return response.json()

            async def health_getter():
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/runtime-health",
                    headers=self._headers(peer),
                )
                response.raise_for_status()
                return response.json()

            if not self._source_v2(manifest):
                await self._apply_source_v1(
                    manifest,
                    snapshot_getter=snapshot_getter,
                    events_getter=events_getter,
                    health_getter=health_getter,
                )
                return

            async def bootstrap_getter():
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/bootstrap",
                    headers=self._headers(peer),
                )
                response.raise_for_status()
                return response.json()

            async def current_page_getter(scope, generation, snapshot_id, cursor, barrier):
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/current/{scope}",
                    headers=self._headers(peer),
                    params={
                        "source_stream_generation": generation,
                        "snapshot_id": snapshot_id,
                        "cursor": cursor,
                        "limit": 100,
                        "barrier_source_seq": barrier,
                    },
                )
                response.raise_for_status()
                return response.json()

            async def current_entity_getter(scope, entity_id, generation):
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/current/{scope}/entity",
                    headers=self._headers(peer),
                    params={
                        "source_stream_generation": generation,
                        "entity_id": entity_id,
                    },
                )
                response.raise_for_status()
                return response.json()

            await self._apply_source_v2(
                manifest,
                bootstrap_getter=bootstrap_getter,
                current_page_getter=current_page_getter,
                current_entity_getter=current_entity_getter,
                events_getter=events_getter,
                health_getter=health_getter,
            )
        except Exception as exc:
            if current is not None:
                await self.store.mark_source(
                    peer.instance_id,
                    current["source_stream_generation"],
                    "unavailable",
                )
                await self.store.clear_runtime_overlay(peer.instance_id)
            status = (
                "OFFLINE_AUTH"
                if isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in {401, 403}
                else "DEGRADED"
            )
            await self._mark_problem_scopes(peer.instance_id, status, type(exc).__name__)

    async def _mark_problem_scopes(
        self, source_node_id: str, status: str, reason: str
    ) -> None:
        scopes = await self.store.source_scopes(source_node_id)
        if scopes:
            await self.store.mark_scope_status(source_node_id, scopes, status, reason)

    async def _sync_follower(self) -> None:
        peer = self.config.peers_by_id.get(self.owner_node_id)
        if peer is None:
            raise FleetProjectionError("projection owner peer is unavailable")
        async with self.client_factory() as client:
            manifest_response = await client.get(
                f"{peer.origin}/internal/fleet/v1/projection/manifest",
                headers=self._headers(peer),
            )
            manifest_response.raise_for_status()
            remote = manifest_response.json()
            if (
                str(remote.get("node_id") or "") != peer.instance_id
                or str(remote.get("owner_node_id") or "") != peer.instance_id
                or remote.get("role") != "owner"
            ):
                raise FleetProjectionError("projection owner identity mismatch")
            local = await self.store.meta()
            if (
                int(remote["projection_epoch"]) != local["projection_epoch"]
                or int(remote["projection_seq"]) < local["projection_seq"]
                or (local["projection_seq"] == 0 and int(remote["projection_seq"]) > 0)
            ):
                await self._follower_snapshot(client, peer)
                local = await self.store.meta()
            pages = 0
            while int(remote["projection_seq"]) > local["projection_seq"]:
                events_response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/projection/events",
                    headers=self._headers(peer),
                    params={
                        "since": local["projection_seq"],
                        "projection_epoch": local["projection_epoch"],
                        "limit": DELTA_BATCH_SIZE,
                    },
                )
                events_response.raise_for_status()
                page = events_response.json()
                if page.get("reset_required"):
                    await self._follower_snapshot(client, peer)
                    local = await self.store.meta()
                    break
                before = local["projection_seq"]
                local = await self.store.apply_owner_events(
                    page,
                    owner_node_id=peer.instance_id,
                )
                pages += 1
                if local["projection_seq"] <= before or pages >= MAX_SYNC_PAGES:
                    raise FleetProjectionError("projection follower catch-up stalled")
            overlay_response = await client.get(
                f"{peer.origin}/internal/fleet/v1/projection/runtime-overlays",
                headers=self._headers(peer),
            )
            overlay_response.raise_for_status()
            overlay_page = overlay_response.json()
            sources = overlay_page.get("sources")
            scope_statuses = overlay_page.get("scope_statuses")
            await self.store.apply_owner_overlays(
                list(overlay_page.get("runtime_overlays") or []),
                owner_node_id=peer.instance_id,
                projection_epoch=int(overlay_page["projection_epoch"]),
                sources=list(sources) if sources is not None else None,
                scope_statuses=(
                    list(scope_statuses) if scope_statuses is not None else None
                ),
            )

    async def _follower_snapshot(self, client, peer) -> None:
        response = await client.get(
            f"{peer.origin}/internal/fleet/v1/projection/snapshot",
            headers=self._headers(peer),
        )
        response.raise_for_status()
        await self.store.apply_owner_snapshot(
            response.json(),
            owner_node_id=peer.instance_id,
        )

    async def _local_node_id(self) -> str | None:
        if self.local_source is None:
            return None
        if self._local_source_node_id:
            return self._local_source_node_id
        manifest = await self.local_source.manifest()
        self._local_source_node_id = str(manifest["node_id"])
        return self._local_source_node_id

    async def _relay(
        self,
        operation: str,
        *,
        source_node_ids: list[str] | None = None,
        resource: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict:
        kwargs = kwargs or {}
        requested = set(source_node_ids or [])
        local_node_id = await self._local_node_id()
        targets: list[tuple[str, Any | None]] = []
        if local_node_id and (not requested or local_node_id in requested):
            targets.append((local_node_id, None))
        for peer in self.config.peers:
            if not requested or peer.instance_id in requested:
                targets.append((peer.instance_id, peer))
        known = {node_id for node_id, _ in targets}
        missing = sorted(requested - known)
        results: list[dict] = [
            {
                "source_node_id": node_id,
                "ok": False,
                "status": "DEGRADED",
                "error": "unknown_source",
            }
            for node_id in missing
        ]

        async def local_call():
            if operation == "query":
                return await self.local_source.query(resource, **kwargs)
            if operation == "detail":
                return await self.local_source.detail(resource, kwargs["entity_id"])
            if operation == "namespaces":
                return await self.local_source.query_namespaces(**kwargs)
            if operation == "task_graph":
                return await self.local_source.task_graph(**kwargs)
            raise ValueError("unknown query relay operation")

        async def remote_call(client, peer):
            if operation == "query":
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/query/{resource}",
                    headers=self._headers(peer),
                    params=_query_params(kwargs),
                )
            elif operation == "detail":
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/detail/{resource}",
                    headers=self._headers(peer),
                    params={"entity_id": kwargs["entity_id"]},
                )
            elif operation == "namespaces":
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/namespaces",
                    headers=self._headers(peer),
                    params=_query_params(kwargs),
                )
            elif operation == "task_graph":
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/task-graph",
                    headers=self._headers(peer),
                    params=kwargs,
                )
            else:
                raise ValueError("unknown query relay operation")
            response.raise_for_status()
            return response.json()

        timeout = max(0.05, float(self.config.request_timeout_seconds))
        async with self.client_factory() as client:

            async def one(node_id, peer):
                started = time.monotonic()
                try:
                    if peer is None:
                        data = await asyncio.wait_for(local_call(), timeout=timeout)
                    else:
                        data = await asyncio.wait_for(
                            remote_call(client, peer), timeout=timeout
                        )
                    self._observe(
                        "query_relay",
                        started,
                        source_node_id=node_id,
                        reason=operation,
                        rows=_relay_rows(data),
                        size=len(str(data)),
                    )
                    return {
                        "source_node_id": node_id,
                        "ok": True,
                        "status": "LIVE",
                        "data": _redact_query_data(
                            data, operation=operation, resource=resource
                        ),
                    }
                except Exception as exc:
                    auth = (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code in {401, 403}
                    )
                    return {
                        "source_node_id": node_id,
                        "ok": False,
                        "status": "OFFLINE_AUTH" if auth else "DEGRADED",
                        "error": type(exc).__name__,
                    }

            results.extend(
                await asyncio.gather(*(one(node_id, peer) for node_id, peer in targets))
            )
        failures = [item for item in results if not item["ok"]]
        return {
            "operation": operation,
            "resource": resource,
            "sources": results,
            "partial": bool(failures) and len(failures) < len(results),
            "complete": not failures,
        }

    async def query(
        self,
        resource: str,
        *,
        source_node_ids: list[str] | None = None,
        **kwargs,
    ) -> dict:
        return await self._relay(
            "query",
            resource=resource,
            source_node_ids=source_node_ids,
            kwargs=kwargs,
        )

    async def detail(
        self,
        resource: str,
        entity_id: str,
        *,
        source_node_ids: list[str] | None = None,
    ) -> dict:
        return await self._relay(
            "detail",
            resource=resource,
            source_node_ids=source_node_ids,
            kwargs={"entity_id": entity_id},
        )

    async def namespaces(
        self,
        *,
        source_node_ids: list[str] | None = None,
        **kwargs,
    ) -> dict:
        return await self._relay(
            "namespaces", source_node_ids=source_node_ids, kwargs=kwargs
        )

    async def task_graph(
        self,
        *,
        namespace: str,
        task_id: str,
        depth: int = 2,
        source_node_ids: list[str] | None = None,
    ) -> dict:
        return await self._relay(
            "task_graph",
            source_node_ids=source_node_ids,
            kwargs={"namespace": namespace, "task_id": task_id, "depth": depth},
        )


def _entity_type_for_scope(scope: str) -> str:
    for entity_type, candidate in _SCOPE_BY_ENTITY.items():
        if candidate == scope:
            return entity_type
    raise FleetProjectionError(f"unknown current scope: {scope}")


def _query_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if key == "filters":
            for filter_key, filter_value in (value or {}).items():
                if filter_value is not None:
                    params[filter_key] = filter_value
        else:
            params[key] = value
    return params


def _relay_rows(data: Any) -> int:
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return len(data["items"])
        if isinstance(data.get("nodes"), list):
            return len(data["nodes"])
        if isinstance(data.get("namespaces"), list):
            return len(data["namespaces"])
        return int(data is not None)
    return 0


def _redact_query_data(data: Any, *, operation: str, resource: str | None) -> Any:
    if not isinstance(data, dict):
        return data
    sensitive = {
        "auth_principal_id",
        "authorization",
        "auth_token",
        "selector_hash",
        "auth_secret_hash",
    }

    def clean(value):
        if isinstance(value, dict):
            result = {
                key: clean(item)
                for key, item in value.items()
                if key not in sensitive
            }
            if operation == "query" and resource == "commands":
                result.pop("cmd", None)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(data)
