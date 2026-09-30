from __future__ import annotations

import asyncio

import httpx

from terminal_mcp.fleet.projection_storage import FleetProjectionError


class FleetProjectionService:
    """Shadow owner/follower synchronization for the derived Fleet read model."""

    def __init__(
        self,
        config,
        store,
        *,
        local_source=None,
        owner_node_id: str,
        client_factory=None,
        interval_seconds: float | None = None,
    ):
        self.config = config
        self.store = store
        self.local_source = local_source
        self.owner_node_id = owner_node_id
        self.client_factory = client_factory or self._default_client
        self.interval_seconds = float(
            interval_seconds or config.replication_interval_seconds
        )
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._local_source_node_id: str | None = None

    def _default_client(self):
        return httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

    def _headers(self, peer) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {peer.auth_token}",
            "X-Terminal-MCP-Peer": self.config.instance_id,
        }

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
        async with self.client_factory() as client:
            for peer in self.config.peers:
                await self._sync_remote_source(client, peer)

    async def _apply_source(
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
            )
            if page.get("reset_required"):
                await self.store.apply_snapshot(await snapshot_getter())
            else:
                await self.store.apply_source_page(page)
        await self.store.put_runtime_overlay(node_id, await health_getter())

    async def _sync_local_source(self) -> None:
        manifest = await self.local_source.manifest()
        self._local_source_node_id = str(manifest["node_id"])

        async def snapshot_getter():
            return await self.local_source.snapshot()

        async def events_getter(since, generation):
            return await self.local_source.events(
                since=since,
                limit=1000,
                source_stream_generation=generation,
            )

        async def health_getter():
            return await self.local_source.runtime_health()

        await self._apply_source(
            manifest,
            snapshot_getter=snapshot_getter,
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

            async def events_getter(since, generation):
                response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/source/events",
                    headers=self._headers(peer),
                    params={
                        "since": since,
                        "limit": 1000,
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

            await self._apply_source(
                manifest,
                snapshot_getter=snapshot_getter,
                events_getter=events_getter,
                health_getter=health_getter,
            )
        except Exception:
            if current is not None:
                await self.store.mark_source(
                    peer.instance_id,
                    current["source_stream_generation"],
                    "unavailable",
                )
                await self.store.clear_runtime_overlay(peer.instance_id)

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
            if int(remote["projection_seq"]) > local["projection_seq"]:
                events_response = await client.get(
                    f"{peer.origin}/internal/fleet/v1/projection/events",
                    headers=self._headers(peer),
                    params={
                        "since": local["projection_seq"],
                        "projection_epoch": local["projection_epoch"],
                        "limit": 1000,
                    },
                )
                events_response.raise_for_status()
                page = events_response.json()
                if page.get("reset_required"):
                    await self._follower_snapshot(client, peer)
                else:
                    await self.store.apply_owner_events(
                        page,
                        owner_node_id=peer.instance_id,
                    )
            overlay_response = await client.get(
                f"{peer.origin}/internal/fleet/v1/projection/runtime-overlays",
                headers=self._headers(peer),
            )
            overlay_response.raise_for_status()
            overlay_page = overlay_response.json()
            await self.store.apply_owner_overlays(
                list(overlay_page.get("runtime_overlays") or []),
                owner_node_id=peer.instance_id,
                projection_epoch=int(overlay_page["projection_epoch"]),
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
