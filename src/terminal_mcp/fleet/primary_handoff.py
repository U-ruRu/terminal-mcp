"""Transfer the Mesh registry without changing its slot records or identities."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.fleet.config import FleetPeer
from terminal_mcp.fleet.control_storage import FleetControlError


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_handoff_certificate(certificate: dict, public_key: str) -> dict:
    try:
        payload = certificate["payload"]
        Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
            _decode(certificate["signature"]), _canonical(payload)
        )
        if set(payload) != {
            "operation_id",
            "mesh_id",
            "from_node_id",
            "to_node_id",
            "to_public_key",
            "previous_revisions",
            "registry_digest",
        }:
            raise ValueError("invalid fields")
        if len(payload["previous_revisions"]) != 4:
            raise ValueError("invalid revisions")
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        # Neither a peer-supplied key nor an unsigned topology field establishes
        # authority. All failures intentionally share a credential-free reason.
        raise FleetControlError("mesh_primary_certificate_invalid") from exc
    return payload


class PrimaryHandoffControl:
    async def _verified_primary_change(self, snapshot: dict) -> bool:
        return await self._verified_primary_certificates(snapshot) is not None

    async def _verified_primary_certificates(self, snapshot: dict) -> list[dict] | None:
        incoming = str(snapshot.get("control_node_id") or "")
        if incoming == self.store.control_node_id:
            return []
        local = await self.snapshot()
        mesh_id = self._member_mesh(local, self.store.node_id)
        if not mesh_id or self._member_mesh(snapshot, self.store.node_id) != mesh_id:
            return None
        authority = self.store.control_node_id
        previous = await self.store.managed_node(authority)
        public_key = str((previous or {}).get("public_key") or "")
        if authority == self.store.node_id:
            public_key = (await self._ensure_local_identity())["public_key"]
        certificates = sorted(
            snapshot.get("_primary_handoffs") or [],
            key=lambda item: item.get("payload", {}).get("previous_revisions", [0])[0],
        )
        accepted = []
        minimum_revision = int(local.get("revisions", {}).get("topology", 0))
        for certificate in certificates:
            hint = certificate.get("payload") or {}
            if hint.get("mesh_id") != mesh_id or hint.get("from_node_id") != authority:
                continue
            signed = verify_handoff_certificate(certificate, public_key)
            checkpoint = int(signed["previous_revisions"][0])
            if checkpoint < minimum_revision:
                raise FleetControlError("mesh_primary_certificate_stale")
            minimum_revision = checkpoint + 1
            if int(snapshot.get("revisions", {}).get("topology", 0)) <= int(
                signed["previous_revisions"][0]
            ):
                raise FleetControlError("mesh_primary_certificate_stale")
            accepted.append(certificate)
            authority = signed["to_node_id"]
            public_key = signed["to_public_key"]
            if authority == incoming:
                advertised_key = next(
                    (
                        node.get("public_key")
                        for node in snapshot.get("nodes", [])
                        if node.get("node_id") == incoming
                    ),
                    None,
                )
                if advertised_key != public_key:
                    raise FleetControlError("mesh_primary_certificate_invalid")
                return accepted
        return None

    async def _primary_peer_call(self, peer, action: str, payload: dict) -> dict:
        async with self.client_factory() as client:
            response = await client.post(
                f"{peer.origin}/internal/fleet/control/primary/{action}",
                headers=self._headers(peer),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        if not body.get("ok"):
            raise FleetControlError(
                str(body.get("code") or "mesh_primary_handoff_pending"),
                blockers=body.get("blockers"),
            )
        return body

    async def change_primary(
        self, *, node_id: str, operation_id: str, expected_topology_revision: int | None = None
    ) -> dict:
        if not operation_id or len(operation_id) > 128:
            raise FleetControlError("mesh_operation_id_invalid")
        # Replay precedes authority checks and CAS. The former primary can confirm
        # a committed operation even after a lost activation response.
        existing = await self.store.primary_handoff(operation_id)
        if existing and existing["to_node_id"] != node_id:
            raise FleetControlError("mesh_operation_id_conflict")
        if existing and existing["phase"] == "completed":
            return await self.snapshot()
        if existing and existing["phase"] == "aborted":
            raise FleetControlError("mesh_operation_aborted")
        if not self.is_control_node and not (
            existing and existing["from_node_id"] == self.store.node_id
        ):
            return await self._forward_mutation(
                "primary",
                {
                    "node_id": node_id,
                    "operation_id": operation_id,
                    "expected_topology_revision": expected_topology_revision,
                },
            )
        if self.native_access is None:
            raise FleetControlError("mesh_registry_unavailable")
        async with self._topology_lock:
            if existing is None:
                state = await self.snapshot()
                mesh_id = self._member_mesh(state, self.store.node_id)
                if mesh_id is None or self._member_mesh(state, node_id) != mesh_id:
                    raise FleetControlError("mesh_primary_member_required")
                if node_id == self.store.node_id:
                    return state
                revisions = state["revisions"]
                if (
                    expected_topology_revision is not None
                    and revisions["topology"] != expected_topology_revision
                ):
                    raise FleetControlError("topology_revision_conflict")
                peer = await self._management_peer(node_id)
                if peer is None:
                    raise FleetControlError("mesh_member_unavailable")
                await self.prepare_local_transition(
                    operation_id=operation_id,
                    target_mesh_id=mesh_id,
                    target_authority_id=node_id,
                    coordinator_id=self.store.node_id,
                    preserve_registry=True,
                )
                registry = await asyncio.to_thread(self.native_access.store.export_registry)
                if registry["namespace_id"] != mesh_id:
                    await self.native_access.abort_topology_transition(operation_id)
                    raise FleetControlError("mesh_registry_namespace_mismatch")
                signed = {
                    "operation_id": operation_id,
                    "mesh_id": mesh_id,
                    "from_node_id": self.store.node_id,
                    "to_node_id": node_id,
                    "to_public_key": peer.public_key,
                    "previous_revisions": [
                        revisions[key] for key in ("topology", "trust", "access_policy", "routing")
                    ],
                    "registry_digest": hashlib.sha256(_canonical(registry)).hexdigest(),
                }
                identity = await self._ensure_local_identity()
                signature = Ed25519PrivateKey.from_private_bytes(
                    _decode(identity["private_key"])
                ).sign(_canonical(signed))
                certificate = {
                    "payload": signed,
                    "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
                }
                members = await self.store.managed_peer_material()
                package = {
                    "registry": registry,
                    "peer": {
                        "instance_id": peer.instance_id,
                        "origin": peer.origin,
                        "public_key": peer.public_key,
                        "auth_token": peer.auth_token,
                    },
                    "members": [
                        {
                            "instance_id": member["node_id"],
                            "origin": member["origin"],
                            "public_key": member["public_key"],
                            "auth_token": member["auth_token"],
                        }
                        for member in members
                        if member.get("mesh_id") == mesh_id and member["node_id"] != node_id
                    ],
                }
                existing = await self.store.prepare_primary_handoff(
                    certificate=certificate, payload=package
                )
            try:
                return await self._resume_primary_handoff(existing)
            except (FleetControlError, AccessMeshError):
                latest = await self.store.primary_handoff(operation_id)
                if latest and latest["phase"] == "prepared":
                    await self._abort_prepared_primary(latest)
                raise

    async def _abort_prepared_primary(self, handoff: dict) -> None:
        operation_id = handoff["operation_id"]
        await self._primary_peer_call(
            FleetPeer(**handoff["payload"]["peer"]),
            "abort",
            {
                "operation_id": operation_id,
            },
        )
        for data in handoff["payload"]["members"]:
            await self._transition_peer_call(
                FleetPeer(**data),
                "abort",
                {
                    "operation_id": operation_id,
                },
            )
        await self.abort_local_transition(
            operation_id=operation_id,
            coordinator_id=self.store.node_id,
        )
        await self.store.mark_primary_handoff(operation_id, "aborted")

    async def _resume_primary_handoff(self, handoff: dict) -> dict:
        operation_id = handoff["operation_id"]
        signed = handoff["certificate"]["payload"]
        peer = FleetPeer(**handoff["payload"]["peer"])
        if handoff["phase"] == "prepared":
            # Fence new registry admission on every participant before snapshot
            # activation, including peers whose registry view is stale.
            for data in handoff["payload"]["members"]:
                await self._transition_peer_call(
                    FleetPeer(**data),
                    "prepare",
                    {
                        "operation_id": operation_id,
                        "target_mesh_id": signed["mesh_id"],
                        "target_authority_id": signed["to_node_id"],
                        "preserve_registry": True,
                    },
                )
            prepared = await self._primary_peer_call(
                peer,
                "prepare",
                {
                    "certificate": handoff["certificate"],
                    "registry": handoff["payload"]["registry"],
                },
            )
            if prepared.get("registry_digest") != signed["registry_digest"]:
                raise FleetControlError("mesh_registry_digest_mismatch")
            await self.store.commit_primary_handoff(operation_id)
            handoff = await self.store.primary_handoff(operation_id)
        state = await self._internal_replication_snapshot(await self.snapshot())
        await self._primary_peer_call(
            peer,
            "activate",
            {
                "operation_id": operation_id,
                "state": state,
            },
        )
        await self.native_access.finish_topology_transition(
            operation_id,
            namespace_id=signed["mesh_id"],
            authority_id=signed["to_node_id"],
            mesh_id=signed["mesh_id"],
            clear_local=False,
        )
        await self.store.complete_topology_transition(self.store.node_id, operation_id)
        await self._apply_local(await self.snapshot())
        await self.store.mark_primary_handoff(operation_id, "completed")
        self._schedule_deferred_reconcile()
        return await self.snapshot()

    async def prepare_primary_import(
        self, *, certificate: dict, registry: dict, source_node_id: str
    ) -> dict:
        current = await self.snapshot()
        source = await self.store.managed_node(source_node_id)
        if source_node_id != self.store.control_node_id or source is None:
            raise FleetControlError("control_authority_required")
        signed = verify_handoff_certificate(certificate, str(source.get("public_key") or ""))
        if (
            signed["from_node_id"] != source_node_id
            or signed["to_node_id"] != self.store.node_id
            or signed["mesh_id"] != self._member_mesh(current, self.store.node_id)
        ):
            raise FleetControlError("mesh_primary_handoff_authority_mismatch")
        if hashlib.sha256(_canonical(registry)).hexdigest() != signed["registry_digest"]:
            raise FleetControlError("mesh_registry_digest_mismatch")
        identity = await self._ensure_local_identity()
        if signed["to_public_key"] != identity["public_key"]:
            raise FleetControlError("mesh_primary_target_key_changed")
        operation_id = signed["operation_id"]
        handoff = await self.store.prepare_primary_handoff(
            certificate=certificate, payload={"registry": registry}
        )
        if handoff["phase"] not in {"imported", "committed", "completed"}:
            await self.native_access.begin_topology_transition(operation_id)
            await asyncio.to_thread(
                self.native_access.store.import_registry,
                registry,
                expected_namespace_id=signed["mesh_id"],
            )
            await self.store.mark_primary_handoff(operation_id, "imported")
        return {
            "ok": True,
            "operation_id": operation_id,
            "registry_digest": signed["registry_digest"],
        }

    async def activate_primary_import(
        self, *, operation_id: str, state: dict, source_node_id: str
    ) -> dict:
        handoff = await self.store.primary_handoff(operation_id)
        if (
            handoff is None
            or handoff["from_node_id"] != source_node_id
            or handoff["to_node_id"] != self.store.node_id
        ):
            raise FleetControlError("mesh_primary_handoff_not_found")
        if handoff["phase"] == "completed":
            return {"ok": True, "control": await self.snapshot()}
        if handoff["phase"] not in {"imported", "committed"}:
            raise FleetControlError("mesh_registry_import_required")
        if state.get(
            "control_node_id"
        ) != self.store.node_id or not await self._verified_primary_change(state):
            raise FleetControlError("mesh_primary_certificate_invalid")
        await self._accept_replica(state, source_node_id=source_node_id)
        await self.store.mark_primary_handoff(operation_id, "committed")
        mesh_id = handoff["mesh_id"]
        await self.native_access.finish_topology_transition(
            operation_id,
            namespace_id=mesh_id,
            authority_id=self.store.node_id,
            mesh_id=mesh_id,
            clear_local=False,
        )
        await self._apply_local(await self.snapshot())
        await self.store.mark_primary_handoff(operation_id, "completed")
        self._schedule_deferred_reconcile()
        return {"ok": True, "control": await self.snapshot()}

    async def abort_primary_import(self, *, operation_id: str, source_node_id: str) -> dict:
        handoff = await self.store.primary_handoff(operation_id)
        if handoff is None:
            return {"ok": True, "status": "absent"}
        if handoff["from_node_id"] != source_node_id:
            raise FleetControlError("control_authority_required")
        if handoff["phase"] in {"committed", "completed"}:
            raise FleetControlError("mesh_transition_already_committed")
        await self.native_access.abort_topology_transition(operation_id)
        await self.store.mark_primary_handoff(operation_id, "aborted")
        return {"ok": True, "status": "aborted"}

    async def _recover_primary_handoffs(self) -> None:
        if self.native_access is None:
            return
        async with self._topology_lock:
            local = await self.store.topology_transition(self.store.node_id)
            if (
                local
                and local["phase"] == "prepared"
                and local["coordinator_id"] == self.store.node_id
                and local["payload"].get("preserve_registry")
                and await self.store.primary_handoff(local["operation_id"]) is None
            ):
                await self.abort_local_transition(
                    operation_id=local["operation_id"],
                    coordinator_id=self.store.node_id,
                )
            for handoff in await self.store.primary_handoffs(pending_only=True):
                if handoff["from_node_id"] == self.store.node_id:
                    await self._resume_primary_handoff(handoff)
                elif handoff["to_node_id"] == self.store.node_id and self.is_control_node:
                    mesh_id = handoff["mesh_id"]
                    await self.native_access.finish_topology_transition(
                        handoff["operation_id"],
                        namespace_id=mesh_id,
                        authority_id=self.store.node_id,
                        mesh_id=mesh_id,
                        clear_local=False,
                    )
                    await self.store.mark_primary_handoff(handoff["operation_id"], "completed")
