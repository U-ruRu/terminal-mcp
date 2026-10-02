from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Iterable

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.control_storage import FleetControlError


class ManagedFleetControl:
    """Managed topology and AccessPolicy on top of the existing Fleet v1 control store."""

    DEFAULT_POLICY = {
        "duration_seconds": 23 * 60,
        "warning_after_seconds": 20 * 60,
        "alert_after_seconds": 22 * 60,
        "rearm_after_seconds": 3 * 60,
        "legacy_admission_enabled": False,
    }

    def __init__(
        self,
        store,
        config: FleetConfig,
        policy_controller,
        *,
        public_base_url: str,
        runtime_targets: Iterable[object] = (),
        client_factory=None,
    ):
        self.store = store
        self.config = config
        self.bootstrap_config = config
        self.policy_controller = policy_controller
        self.public_base_url = public_base_url.rstrip("/")
        self.runtime_targets = tuple(target for target in runtime_targets if target is not None)
        self.client_factory = client_factory or self._default_client
        self._reconcile_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self, interval_seconds: float | None = None) -> None:
        if not self.is_control_node or self._reconcile_task is not None:
            return
        self._stopped.clear()
        interval = max(
            5.0,
            float(
                self.config.replication_interval_seconds
                if interval_seconds is None
                else interval_seconds
            ),
        )
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(interval),
            name="managed-fleet-control-reconciler",
        )

    async def stop(self) -> None:
        self._stopped.set()
        task = self._reconcile_task
        self._reconcile_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def reconcile_pending(self) -> dict:
        state = await self.snapshot()
        if state.get("managed"):
            # Durable managed state is also the source of truth for the live
            # runtime config. Re-apply it even when remote revisions are
            # already converged so a restarted/drifted runtime cannot fall
            # back to bootstrap peer credentials indefinitely.
            await self._apply_local(state)
        if not self.is_control_node or not state.get("managed"):
            return state
        pending = any(
            node.get("state") != "detached"
            and (
                node.get("last_error")
                or int(node.get("desired_topology_revision") or 0)
                != int(node.get("applied_topology_revision") or 0)
                or int(node.get("desired_trust_revision") or 0)
                != int(node.get("applied_trust_revision") or 0)
                or int(node.get("desired_policy_revision") or 0)
                != int(node.get("applied_policy_revision") or 0)
            )
            for node in state.get("nodes") or []
            if node.get("node_id") != self.store.node_id
        )
        return await self.replicate() if pending else state

    async def _reconcile_loop(self, interval_seconds: float) -> None:
        while not self._stopped.is_set():
            await asyncio.sleep(interval_seconds)
            try:
                await self.reconcile_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    def _default_client(self):
        return httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

    def _headers(self, peer: FleetPeer) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {peer.auth_token}",
            "X-Terminal-MCP-Peer": self.config.instance_id,
        }

    @property
    def is_control_node(self) -> bool:
        return self.store.node_id == self.store.control_node_id

    @staticmethod
    def _bearer_token(authorization: str) -> str | None:
        prefix = "bearer "
        if not authorization.lower().startswith(prefix):
            return None
        token = authorization[len(prefix) :].strip()
        return token or None

    async def enrollment_descriptor(self) -> dict:
        identity = await self._ensure_local_identity()
        return {
            "node_id": self.store.node_id,
            "origin": self.public_base_url,
            "public_key": identity["public_key"],
            "auth_token": identity["ingress_token"],
        }

    async def authenticate_management_peer(
        self,
        peer_instance_id: str,
        authorization: str,
        *,
        first_apply_control_node_id: str | None = None,
    ) -> str | None:
        token = self._bearer_token(authorization)
        if token is None:
            return None

        identity = await self._ensure_local_identity()
        ingress_token = str(identity.get("ingress_token") or "")
        if ingress_token and secrets.compare_digest(token, ingress_token):
            if peer_instance_id == self.store.control_node_id:
                return peer_instance_id
            node = await self.store.managed_node(peer_instance_id)
            if node is not None and node.get("state") != "detached":
                return peer_instance_id
            if first_apply_control_node_id and peer_instance_id == first_apply_control_node_id:
                return peer_instance_id

        # Rolling-upgrade fallback for nodes that still rely on bootstrap peer tokens.
        peer = self.bootstrap_config.peers_by_id.get(peer_instance_id)
        if peer is not None and secrets.compare_digest(token, peer.auth_token):
            return peer_instance_id
        return None

    async def _management_peer(self, node_id: str) -> FleetPeer | None:
        material = await self.store.managed_node(node_id)
        if (
            material is not None
            and material.get("state") != "detached"
            and all(
                (material.get("origin"), material.get("public_key"), material.get("auth_token"))
            )
        ):
            return FleetPeer(
                node_id,
                str(material["origin"]),
                str(material["public_key"]),
                str(material["auth_token"]),
            )
        return self.bootstrap_config.peers_by_id.get(node_id)

    async def _forward_mutation_to(
        self, node_id: str, operation: str, payload: dict | None = None
    ) -> dict:
        peer = await self._management_peer(node_id)
        if peer is None:
            raise FleetControlError("control_authority_unavailable")
        async with self.client_factory() as client:
            response = await client.post(
                f"{peer.origin}/internal/fleet/control/mutate/{operation}",
                headers=self._headers(peer),
                json={"payload": payload or {}},
            )
            response.raise_for_status()
            body = response.json()
        if not body.get("ok"):
            raise FleetControlError(str(body.get("code") or "control_mutation_failed"))
        state = body.get("control")
        if not isinstance(state, dict):
            raise FleetControlError("control_mutation_invalid")
        return await self.apply_replica(state, source_node_id=node_id)

    async def _forward_mutation(self, operation: str, payload: dict | None = None) -> dict:
        return await self._forward_mutation_to(self.store.control_node_id, operation, payload)

    async def execute_forwarded(
        self, operation: str, payload: dict, *, authenticated_peer_id: str | None = None
    ) -> dict:
        if operation == "adopt":
            return await self.adopt(
                mesh_id=payload.get("mesh_id"),
                display_name=str(payload.get("display_name") or "Fleet"),
                control_node_id=payload.get("control_node_id"),
            )
        if not self.is_control_node:
            raise FleetControlError("control_authority_required")
        if operation == "delete-mesh":
            return await self.delete_mesh(
                mesh_id=payload.get("mesh_id"),
                expected_topology_revision=payload.get("expected_topology_revision"),
            )
        if operation == "rename":
            return await self.rename(
                str(payload.get("display_name") or ""),
                mesh_id=payload.get("mesh_id"),
                expected_topology_revision=payload.get("expected_topology_revision"),
            )
        if operation == "upsert-node":
            return await self.upsert_node(
                node_id=str(payload.get("node_id") or ""),
                mesh_id=str(payload.get("mesh_id") or ""),
                origin=payload.get("origin"),
                public_key=payload.get("public_key"),
                auth_token=payload.get("auth_token"),
                expected_topology_revision=payload.get("expected_topology_revision"),
            )
        if operation == "detach-node":
            return await self.detach_node(
                str(payload.get("node_id") or ""),
                expected_topology_revision=payload.get("expected_topology_revision"),
            )
        if operation == "move-node":
            return await self.move_node(
                str(payload.get("node_id") or ""),
                str(payload.get("target_mesh_id") or ""),
                expected_topology_revision=payload.get("expected_topology_revision"),
            )
        if operation == "update-policy":
            return await self.update_policy(
                duration_seconds=int(payload["duration_seconds"]),
                warning_after_seconds=int(payload["warning_after_seconds"]),
                alert_after_seconds=int(payload["alert_after_seconds"]),
                rearm_after_seconds=int(payload["rearm_after_seconds"]),
                legacy_admission_enabled=bool(payload["legacy_admission_enabled"]),
                expected_revision=payload.get("expected_revision"),
            )
        if operation == "reset-policy":
            return await self.reset_policy(expected_revision=payload.get("expected_revision"))
        if operation == "rotate-trust":
            node_id = str(payload.get("node_id") or "")
            if authenticated_peer_id is not None and authenticated_peer_id != node_id:
                raise FleetControlError("trust_rotation_peer_mismatch")
            return await self._publish_node_public_key(
                node_id=node_id,
                public_key=str(payload.get("public_key") or ""),
                expected_trust_revision=payload.get("expected_trust_revision"),
            )
        if operation == "reconcile":
            return await self.replicate()
        raise FleetControlError("control_operation_invalid")

    def _replace_runtime_config(self, config: FleetConfig) -> None:
        self.config = config
        for target in self.runtime_targets:
            if hasattr(target, "config"):
                target.config = config

    def _sync_control_node(self, control_node_id: str) -> None:
        for target in self.runtime_targets:
            if hasattr(target, "control_node_id"):
                target.control_node_id = control_node_id

    async def _ensure_local_identity(self) -> dict:
        identity = await self.store.ensure_managed_identity(self.config.signing_private_key)
        if identity["private_key"] != self.config.signing_private_key:
            self._replace_runtime_config(
                FleetConfig(
                    self.config.instance_id,
                    identity["private_key"],
                    self.config.peers,
                    self.config.replication_interval_seconds,
                    self.config.request_timeout_seconds,
                    self.config.local_auth_token,
                )
            )
        return identity

    async def _activate_committed_identity(self) -> dict:
        identity = await self.store.managed_identity()
        if identity is None:
            raise FleetControlError("managed identity unavailable")
        self._replace_runtime_config(
            FleetConfig(
                self.config.instance_id,
                identity["private_key"],
                self.config.peers,
                self.config.replication_interval_seconds,
                self.config.request_timeout_seconds,
                self.config.local_auth_token,
            )
        )
        return identity

    def _local_public_key(self) -> str:
        raw = (
            Ed25519PrivateKey.from_private_bytes(
                base64.urlsafe_b64decode(
                    self.config.signing_private_key
                    + "=" * (-len(self.config.signing_private_key) % 4)
                )
            )
            .public_key()
            .public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _initial_control_node(self) -> list[dict]:
        # Bootstrap peers are transport hints only. Creating a managed Mesh must never
        # turn configured peers into members implicitly; membership is an explicit
        # control-plane mutation.
        return [
            {
                "node_id": self.config.instance_id,
                "origin": self.public_base_url,
                "public_key": self._local_public_key(),
            }
        ]

    async def snapshot(self) -> dict:
        return await self.store.control_state(include_secrets=False)

    @staticmethod
    def _resolve_mesh_id(state: dict, mesh_id: str | None) -> str:
        if mesh_id:
            return str(mesh_id)
        local = state.get("mesh") or {}
        if local.get("mesh_id"):
            return str(local["mesh_id"])
        meshes = state.get("meshes") or []
        if len(meshes) == 1:
            return str(meshes[0]["mesh_id"])
        raise FleetControlError("mesh_id_required")

    async def adopt(
        self,
        *,
        mesh_id: str | None = None,
        display_name: str = "Fleet",
        control_node_id: str | None = None,
    ) -> dict:
        await self._ensure_local_identity()
        requested_control = str(control_node_id or self.store.control_node_id)
        if requested_control != self.store.node_id:
            return await self._forward_mutation_to(
                requested_control,
                "adopt",
                {
                    "mesh_id": mesh_id,
                    "display_name": display_name,
                    "control_node_id": requested_control,
                },
            )
        if not self.is_control_node:
            await self.store.claim_local_control_authority()
            self._sync_control_node(self.store.control_node_id)
        state = await self.store.adopt_managed(
            mesh_id=mesh_id or f"mesh-{secrets.token_hex(8)}",
            display_name=display_name,
            nodes=self._initial_control_node(),
            policy=self.policy_controller.snapshot(),
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def delete_mesh(
        self,
        *,
        mesh_id: str | None = None,
        expected_topology_revision: int | None = None,
    ) -> dict:
        state = await self.snapshot()
        target_mesh_id = self._resolve_mesh_id(state, mesh_id)
        if not self.is_control_node:
            return await self._forward_mutation(
                "delete-mesh",
                {
                    "mesh_id": target_mesh_id,
                    "expected_topology_revision": expected_topology_revision,
                },
            )
        state = await self.store.delete_managed_mesh(
            target_mesh_id,
            expected_topology_revision=expected_topology_revision,
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def rename(
        self,
        display_name: str,
        *,
        mesh_id: str | None = None,
        expected_topology_revision: int | None = None,
    ) -> dict:
        state = await self.snapshot()
        target_mesh_id = self._resolve_mesh_id(state, mesh_id)
        if not self.is_control_node:
            return await self._forward_mutation(
                "rename",
                {
                    "mesh_id": target_mesh_id,
                    "display_name": display_name,
                    "expected_topology_revision": expected_topology_revision,
                },
            )
        state = await self.store.rename_managed_mesh(
            target_mesh_id,
            display_name,
            expected_topology_revision=expected_topology_revision,
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def upsert_node(
        self,
        *,
        node_id: str,
        mesh_id: str,
        origin: str | None,
        public_key: str | None,
        auth_token: str | None,
        expected_topology_revision: int | None = None,
    ) -> dict:
        if not self.is_control_node:
            return await self._forward_mutation(
                "upsert-node",
                {
                    "node_id": node_id,
                    "mesh_id": mesh_id,
                    "origin": origin,
                    "public_key": public_key,
                    "auth_token": auth_token,
                    "expected_topology_revision": expected_topology_revision,
                },
            )
        bootstrap = self.bootstrap_config.peers_by_id.get(node_id)
        state = await self.store.upsert_managed_node(
            node_id=node_id,
            mesh_id=mesh_id,
            origin=origin or (bootstrap.origin if bootstrap else None),
            public_key=public_key or (bootstrap.public_key if bootstrap else None),
            auth_token=auth_token or (bootstrap.auth_token if bootstrap else None),
            expected_topology_revision=expected_topology_revision,
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def detach_node(
        self, node_id: str, *, expected_topology_revision: int | None = None
    ) -> dict:
        if not self.is_control_node:
            return await self._forward_mutation(
                "detach-node",
                {
                    "node_id": node_id,
                    "expected_topology_revision": expected_topology_revision,
                },
            )
        state = await self.store.detach_managed_node(
            node_id,
            expected_topology_revision=expected_topology_revision,
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def move_node(
        self,
        node_id: str,
        target_mesh_id: str,
        *,
        expected_topology_revision: int | None = None,
    ) -> dict:
        if not self.is_control_node:
            return await self._forward_mutation(
                "move-node",
                {
                    "node_id": node_id,
                    "target_mesh_id": target_mesh_id,
                    "expected_topology_revision": expected_topology_revision,
                },
            )
        state = await self.store.move_managed_node(
            node_id,
            target_mesh_id,
            expected_topology_revision=expected_topology_revision,
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def update_policy(
        self,
        *,
        duration_seconds: int,
        warning_after_seconds: int,
        alert_after_seconds: int,
        rearm_after_seconds: int,
        legacy_admission_enabled: bool,
        expected_revision: int | None = None,
    ) -> dict:
        if not self.is_control_node:
            return await self._forward_mutation(
                "update-policy",
                {
                    "duration_seconds": duration_seconds,
                    "warning_after_seconds": warning_after_seconds,
                    "alert_after_seconds": alert_after_seconds,
                    "rearm_after_seconds": rearm_after_seconds,
                    "legacy_admission_enabled": legacy_admission_enabled,
                    "expected_revision": expected_revision,
                },
            )
        await self.store.update_access_policy(
            duration_seconds=duration_seconds,
            warning_after_seconds=warning_after_seconds,
            alert_after_seconds=alert_after_seconds,
            rearm_after_seconds=rearm_after_seconds,
            legacy_admission_enabled=legacy_admission_enabled,
            expected_revision=expected_revision,
        )
        state = await self.snapshot()
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def _publish_node_public_key(
        self,
        *,
        node_id: str,
        public_key: str,
        expected_trust_revision: int | None = None,
    ) -> dict:
        if not self.is_control_node:
            raise FleetControlError("control_authority_required")
        state = await self.store.update_managed_node_public_key(
            node_id,
            public_key=public_key,
            expected_trust_revision=expected_trust_revision,
        )
        await self._apply_local(state)
        await self.replicate()
        return await self.snapshot()

    async def rotate_local_trust(self, *, expected_trust_revision: int | None = None) -> dict:
        state = await self.snapshot()
        if not state.get("managed"):
            raise FleetControlError("managed_control_not_adopted")
        pending = await self.store.prepare_managed_identity_rotation()
        expected = (
            int(state["revisions"]["trust"])
            if expected_trust_revision is None
            else int(expected_trust_revision)
        )
        if self.is_control_node:
            state = await self._publish_node_public_key(
                node_id=self.store.node_id,
                public_key=pending["public_key"],
                expected_trust_revision=expected,
            )
        else:
            state = await self._forward_mutation(
                "rotate-trust",
                {
                    "node_id": self.store.node_id,
                    "public_key": pending["public_key"],
                    "expected_trust_revision": expected,
                },
            )
        await self.store.commit_managed_identity_rotation(pending["generation"])
        await self._activate_committed_identity()
        return state

    async def reset_policy(self, *, expected_revision: int | None = None) -> dict:
        if not self.is_control_node:
            return await self._forward_mutation(
                "reset-policy", {"expected_revision": expected_revision}
            )
        return await self.update_policy(
            **self.DEFAULT_POLICY,
            expected_revision=expected_revision,
        )

    async def reconcile_local(self) -> dict:
        await self._ensure_local_identity()
        state = await self.snapshot()
        self._sync_control_node(str(state["control_node_id"]))
        if not state["managed"]:
            await self._restore_local_policy()
            return state
        await self._apply_local(state)
        return await self.snapshot()

    async def apply_replica(self, snapshot: dict, *, source_node_id: str) -> dict:
        incoming_control = str(snapshot.get("control_node_id") or "")
        if source_node_id != self.store.control_node_id and source_node_id != incoming_control:
            raise FleetControlError("control_authority_required")
        internal_material = snapshot.get("_peer_material") or []
        managed_tokens = {
            str(item.get("node_id") or ""): str(item.get("auth_token") or "")
            for item in internal_material
            if item.get("node_id") and item.get("auth_token")
        }
        public_snapshot = dict(snapshot)
        public_snapshot.pop("_peer_material", None)
        bootstrap_tokens = {
            peer.instance_id: peer.auth_token for peer in self.bootstrap_config.peers
        }
        bootstrap_tokens.update(managed_tokens)
        state = await self.store.apply_managed_replica(
            public_snapshot,
            bootstrap_tokens=bootstrap_tokens,
        )
        self._sync_control_node(self.store.control_node_id)
        await self._apply_local(state)
        return await self.snapshot()

    async def _restore_local_policy(self) -> None:
        restore = getattr(self.policy_controller, "restore_local", None)
        if restore is not None:
            await restore()

    def _restore_bootstrap_runtime(self, *, use_peers: bool = True) -> None:
        config = FleetConfig(
            self.bootstrap_config.instance_id,
            self.config.signing_private_key,
            self.bootstrap_config.peers if use_peers else (),
            self.bootstrap_config.replication_interval_seconds,
            self.bootstrap_config.request_timeout_seconds,
            None,
        )
        self._replace_runtime_config(config)

    async def _apply_local(self, state: dict) -> None:
        if not state.get("managed"):
            await self._restore_local_policy()
            self._restore_bootstrap_runtime(use_peers=state.get("mesh") is None)
            return

        all_nodes = state.get("nodes") or []
        local_node = next(
            (node for node in all_nodes if node.get("node_id") == self.config.instance_id),
            None,
        )
        local_mesh_id = (
            local_node.get("mesh_id")
            if local_node and local_node.get("state") != "detached"
            else None
        )
        policy = state.get("policy")
        if local_mesh_id is None:
            await self._restore_local_policy()
        elif policy:
            await self.policy_controller.apply_managed(
                duration_seconds=int(policy["duration_seconds"]),
                warning_after_seconds=int(policy["warning_after_seconds"]),
                alert_after_seconds=int(policy["alert_after_seconds"]),
                rearm_after_seconds=int(policy["rearm_after_seconds"]),
                legacy_admission_enabled=bool(policy["legacy_admission_enabled"]),
            )
        active_nodes = [
            node
            for node in all_nodes
            if local_mesh_id is not None
            and (
                node.get("mesh_id") == local_mesh_id
                or node.get("node_id") == self.store.control_node_id
            )
            and node.get("state") != "detached"
            and node.get("node_id") != self.config.instance_id
        ]
        material = {item["node_id"]: item for item in await self.store.managed_peer_material()}
        missing = [node["node_id"] for node in active_nodes if node["node_id"] not in material]
        revisions = state.get("revisions") or {}
        if missing:
            await self.store.mark_managed_applied(
                self.config.instance_id,
                policy_revision=int(revisions.get("access_policy") or 0),
                error="trust_material_missing:" + ",".join(sorted(missing)),
            )
            return

        peers = tuple(
            FleetPeer(
                node_id,
                str(material[node_id]["origin"]),
                str(material[node_id]["public_key"]),
                str(material[node_id]["auth_token"]),
            )
            for node_id in sorted(material)
            if any(node["node_id"] == node_id for node in active_nodes)
        )
        identity = await self._ensure_local_identity()
        local_material = await self.store.managed_node(self.config.instance_id)
        local_auth_token = (
            str(local_material.get("auth_token") or "") if local_material else ""
        ) or str(identity["ingress_token"])
        new_config = FleetConfig(
            self.config.instance_id,
            self.config.signing_private_key,
            peers,
            self.config.replication_interval_seconds,
            self.config.request_timeout_seconds,
            local_auth_token,
        )
        self._replace_runtime_config(new_config)
        await self.store.mark_managed_applied(
            self.config.instance_id,
            topology_revision=int(revisions.get("topology") or 0),
            trust_revision=int(revisions.get("trust") or 0),
            policy_revision=int(revisions.get("access_policy") or 0),
            error=None,
        )

    async def _internal_replication_snapshot(self, state: dict) -> dict:
        identity = await self._ensure_local_identity()
        materials = list(await self.store.managed_peer_material())
        local_node = next(
            (
                node
                for node in state.get("nodes") or []
                if node.get("node_id") == self.store.node_id
            ),
            None,
        )
        materials.append(
            {
                "node_id": self.store.node_id,
                "origin": self.public_base_url,
                "public_key": identity["public_key"],
                "auth_token": identity["ingress_token"],
                "mesh_id": local_node.get("mesh_id") if local_node else None,
            }
        )
        internal = dict(state)
        internal["_peer_material"] = materials
        return internal

    async def replicate(
        self,
        *,
        state_override: dict | None = None,
        peers_override: tuple[FleetPeer, ...] | None = None,
    ) -> dict:
        if not self.is_control_node:
            return await self._forward_mutation("reconcile")
        state = state_override or await self.store.control_state(include_secrets=False)
        if not state.get("managed"):
            return state
        revisions = state.get("revisions") or {}
        if peers_override is None:
            material = await self.store.managed_peer_material()
            peers = tuple(
                FleetPeer(
                    str(item["node_id"]),
                    str(item["origin"]),
                    str(item["public_key"]),
                    str(item["auth_token"]),
                )
                for item in material
            )
        else:
            peers = tuple(peers_override)
        if not peers:
            return state
        internal_state = await self._internal_replication_snapshot(state)
        async with self.client_factory() as client:
            for peer in peers:
                try:
                    response = await client.post(
                        f"{peer.origin}/internal/fleet/control/apply",
                        headers=self._headers(peer),
                        json={"state": internal_state},
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not body.get("ok"):
                        raise RuntimeError(str(body.get("code") or "control_apply_failed"))
                    await self.store.mark_managed_applied(
                        peer.instance_id,
                        topology_revision=int(revisions.get("topology") or 0),
                        trust_revision=int(revisions.get("trust") or 0),
                        policy_revision=int(revisions.get("access_policy") or 0),
                        error=None,
                    )
                except Exception as exc:
                    try:
                        await self.store.mark_managed_applied(
                            peer.instance_id,
                            error=f"reconcile_failed:{exc.__class__.__name__}",
                        )
                    except FleetControlError:
                        pass
        return await self.snapshot()
