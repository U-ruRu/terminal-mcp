"""Membership preparation and recovery across the topology and ACS stores."""

from __future__ import annotations

import hashlib

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.fleet.config import FleetPeer
from terminal_mcp.fleet.control_storage import FleetControlError
from terminal_mcp.fleet.protocol import validate_protocol_id


class TopologyControl:
    @staticmethod
    def _member_mesh(state: dict, node_id: str) -> str | None:
        return next((
            node.get("mesh_id") for node in state.get("nodes", [])
            if node.get("node_id") == node_id and node.get("state") != "detached"
        ), None)

    async def prepare_local_transition(
        self, *, operation_id: str, target_mesh_id: str | None,
        target_authority_id: str, coordinator_id: str,
    ) -> dict:
        if not operation_id or len(operation_id) > 128:
            raise FleetControlError("mesh_operation_id_invalid")
        if target_mesh_id is not None:
            validate_protocol_id(target_mesh_id, "mesh_id")
        validate_protocol_id(target_authority_id, "node_id")
        state = await self.snapshot()
        previous_mesh = self._member_mesh(state, self.store.node_id)
        if previous_mesh != target_mesh_id and self.is_control_node and any(
            node.get("mesh_id") == previous_mesh and node.get("state") != "detached"
            and node.get("node_id") != self.store.node_id
            for node in state.get("nodes", [])
        ):
            raise FleetControlError("mesh_primary_handoff_required")
        transition = await self.store.prepare_topology_transition(
            node_id=self.store.node_id, operation_id=operation_id,
            target_mesh_id=target_mesh_id, target_authority_id=target_authority_id,
            coordinator_id=coordinator_id, payload={"incoming": True},
        )
        if transition["phase"] == "completed":
            return {"ok": True, "operation_id": operation_id, "status": "completed"}
        try:
            if self.native_access is not None:
                await self.native_access.begin_topology_transition(operation_id)
        except AccessMeshError as exc:
            await self.store.complete_topology_transition(
                self.store.node_id, operation_id, "aborted"
            )
            raise FleetControlError(exc.code, blockers=getattr(exc, "blockers", [])) from exc
        return {"ok": True, "operation_id": operation_id, "status": "prepared"}

    async def abort_local_transition(self, *, operation_id: str, coordinator_id: str) -> dict:
        transition = await self.store.topology_transition(self.store.node_id)
        if transition is None or transition["operation_id"] != operation_id:
            return {"ok": True, "operation_id": operation_id, "status": "absent"}
        if transition["coordinator_id"] != coordinator_id:
            raise FleetControlError("mesh_transition_coordinator_mismatch")
        if transition["phase"] == "committed":
            raise FleetControlError("mesh_transition_already_committed")
        if transition["phase"] == "completed":
            return {"ok": True, "operation_id": operation_id, "status": "completed"}
        if self.native_access is not None:
            await self.native_access.abort_topology_transition(operation_id)
        await self.store.complete_topology_transition(self.store.node_id, operation_id, "aborted")
        return {"ok": True, "operation_id": operation_id, "status": "aborted"}

    async def _transition_peer_call(self, peer, action: str, payload: dict) -> dict:
        async with self.client_factory() as client:
            response = await client.post(
                f"{peer.origin}/internal/fleet/control/transition/{action}",
                headers=self._headers(peer), json=payload,
            )
            response.raise_for_status()
            body = response.json()
        if not body.get("ok"):
            raise FleetControlError(
                str(body.get("code") or "mesh_transition_unconfirmed"),
                blockers=body.get("blockers"),
            )
        return body

    async def _membership_change(
        self, *, node_id: str, target_mesh_id: str | None, mutate,
        peer: FleetPeer | None = None,
    ) -> dict:
        """Prepare the affected node before authoritatively changing its membership."""
        async with self._topology_lock:
            before = await self.snapshot()
            old_mesh = self._member_mesh(before, node_id)
            existing_node = next((
                row for row in before.get("nodes", []) if row.get("node_id") == node_id
            ), None)
            if existing_node and old_mesh == target_mesh_id:
                return await mutate()
            authority = self.store.node_id if target_mesh_id else node_id
            identity = f"{self.store.node_id}:{node_id}:{target_mesh_id}:" \
                       f"{before.get('revisions', {}).get('topology', 0)}"
            operation_id = "membership-" + hashlib.sha256(identity.encode()).hexdigest()
            existing = await self.store.topology_transition(node_id)
            if existing and existing["phase"] in {"prepared", "committed"}:
                if existing["target_mesh_id"] != target_mesh_id:
                    raise FleetControlError("mesh_transition_pending")
                operation_id = existing["operation_id"]
            if node_id != self.store.node_id:
                peer = peer or await self._management_peer(node_id)
                if peer is None:
                    raise FleetControlError("mesh_member_unavailable")
            await self.store.prepare_topology_transition(
                node_id=node_id, operation_id=operation_id,
                target_mesh_id=target_mesh_id, target_authority_id=authority,
                coordinator_id=self.store.node_id,
                payload={"peer": {
                    "instance_id": peer.instance_id, "origin": peer.origin,
                    "public_key": peer.public_key, "auth_token": peer.auth_token,
                }} if peer else {"incoming": True},
            )
            request = {
                "operation_id": operation_id, "target_mesh_id": target_mesh_id,
                "target_authority_id": authority,
            }
            try:
                if peer is not None and self.native_access is not None:
                    # The authoritative registry can be ahead of this member's
                    # replica. Freeze issuance at the owner as well as work on
                    # the affected node; neither guard changes a slot record.
                    await self.native_access.begin_topology_transition(operation_id)
                if peer is None:
                    await self.prepare_local_transition(
                        **request, coordinator_id=self.store.node_id
                    )
                else:
                    await self._transition_peer_call(peer, "prepare", request)
                state = await mutate()
            except (FleetControlError, ValueError, AccessMeshError) as exc:
                if peer is None:
                    await self.abort_local_transition(
                        operation_id=operation_id, coordinator_id=self.store.node_id
                    )
                else:
                    await self._transition_peer_call(peer, "abort", {
                        "operation_id": operation_id,
                    })
                    await self.store.complete_topology_transition(node_id, operation_id, "aborted")
                    if self.native_access is not None:
                        await self.native_access.abort_topology_transition(operation_id)
                if isinstance(exc, AccessMeshError):
                    raise FleetControlError(
                        exc.code, blockers=getattr(exc, "blockers", [])
                    ) from exc
                raise
            await self.store.complete_topology_transition(node_id, operation_id, "committed")
            return state

    async def _finish_local_membership(self, state: dict) -> None:
        transition = await self.store.topology_transition(self.store.node_id)
        if transition is None or transition["phase"] not in {"prepared", "committed"}:
            return
        mesh_id = self._member_mesh(state, self.store.node_id)
        if mesh_id != transition["target_mesh_id"]:
            return
        # The accepted topology commits first; admission remains durably fenced
        # until the local namespace purge and scope installation commit together.
        await self.store.complete_topology_transition(
            self.store.node_id, transition["operation_id"], "committed"
        )
        if self.native_access is not None:
            await self.native_access.finish_topology_transition(
                transition["operation_id"], namespace_id=mesh_id or self.store.fleet_id,
                authority_id=state["control_node_id"] if mesh_id else self.store.node_id,
                mesh_id=mesh_id, clear_local=True,
            )
        await self.store.complete_topology_transition(
            self.store.node_id, transition["operation_id"]
        )

    async def _recover_membership_changes(self) -> None:
        async with self._topology_lock:
            state = await self.snapshot()
            for transition in await self.store.pending_topology_transitions():
                node_id = transition["node_id"]
                if node_id == self.store.node_id:
                    await self._finish_local_membership(state)
                    continue
                if transition["coordinator_id"] != self.store.node_id:
                    continue
                current_mesh = self._member_mesh(state, node_id)
                if current_mesh == transition["target_mesh_id"]:
                    await self.store.complete_topology_transition(
                        node_id, transition["operation_id"], "committed"
                    )
                    continue  # The ordinary reconciler delivers the committed snapshot.
                if transition["phase"] == "prepared":
                    peer = FleetPeer(**transition["payload"]["peer"])
                    await self._transition_peer_call(peer, "abort", {
                        "operation_id": transition["operation_id"],
                    })
                    await self.store.complete_topology_transition(
                        node_id, transition["operation_id"], "aborted"
                    )
                    if self.native_access is not None:
                        await self.native_access.abort_topology_transition(
                            transition["operation_id"]
                        )
