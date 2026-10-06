"""Canonical operator and authenticated peer control-plane application boundary."""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError
from terminal_mcp.application.operator import OperatorApplication
from terminal_mcp.core.persistent_policy import PersistentPolicyError
from terminal_mcp.fleet.control_storage import FleetControlError


class FleetControlApplication:
    def __init__(self, controller):
        self._controller = controller

    @staticmethod
    async def _mutation(call):
        try:
            return {"ok": True, "control": await call}
        except PersistentPolicyError as exc:
            result = {"ok": False, "code": exc.code, "error": exc.code}
            if exc.blockers:
                result["blockers"] = exc.blockers
            return result
        except (FleetControlError, ValueError) as exc:
            return {"ok": False, "code": str(exc), "error": str(exc)}

    async def state(self, actor: ActorContext):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return {"ok": True, "control": await self._controller.snapshot()}

    async def enrollment(self, actor: ActorContext):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return {"ok": True, "enrollment": await self._controller.enrollment_descriptor()}

    async def adopt(
        self,
        actor: ActorContext,
        *,
        mesh_id: str | None,
        display_name: str,
        control_node_id: str | None,
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.adopt(
                    mesh_id=mesh_id, display_name=display_name, control_node_id=control_node_id
                )
            )

    async def delete_mesh(
        self, actor: ActorContext, *, mesh_id: str, expected_topology_revision: int | None
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.delete_mesh(
                    mesh_id=mesh_id, expected_topology_revision=expected_topology_revision
                )
            )

    async def rename(
        self,
        actor: ActorContext,
        *,
        mesh_id: str,
        display_name: str,
        expected_topology_revision: int | None,
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.rename(
                    display_name,
                    mesh_id=mesh_id,
                    expected_topology_revision=expected_topology_revision,
                )
            )

    async def upsert_node(
        self,
        actor: ActorContext,
        *,
        node_id: str,
        mesh_id: str,
        origin: str | None,
        public_key: str | None,
        auth_token: str | None,
        expected_topology_revision: int | None,
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.upsert_node(
                    node_id=node_id,
                    mesh_id=mesh_id,
                    origin=origin,
                    public_key=public_key,
                    auth_token=auth_token,
                    expected_topology_revision=expected_topology_revision,
                )
            )

    async def detach_node(
        self, actor: ActorContext, *, node_id: str, expected_topology_revision: int | None
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            try:
                control, mutation = await self._controller.detach_node_result(
                    node_id, expected_topology_revision=expected_topology_revision
                )
                return {"ok": True, "control": control, "mutation": mutation}
            except PersistentPolicyError as exc:
                result = {"ok": False, "code": exc.code, "error": exc.code}
                if exc.blockers:
                    result["blockers"] = exc.blockers
                return result
            except (FleetControlError, ValueError) as exc:
                return {"ok": False, "code": str(exc), "error": str(exc)}

    async def move_node(
        self,
        actor: ActorContext,
        *,
        node_id: str,
        target_mesh_id: str,
        expected_topology_revision: int | None,
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.move_node(
                    node_id, target_mesh_id, expected_topology_revision=expected_topology_revision
                )
            )

    async def policy(
        self,
        actor: ActorContext,
        *,
        duration_seconds: int,
        warning_after_seconds: int,
        alert_after_seconds: int,
        rearm_after_seconds: int,
        legacy_admission_enabled: bool,
        expected_revision: int | None,
    ):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.update_policy(
                    duration_seconds=duration_seconds,
                    warning_after_seconds=warning_after_seconds,
                    alert_after_seconds=alert_after_seconds,
                    rearm_after_seconds=rearm_after_seconds,
                    legacy_admission_enabled=legacy_admission_enabled,
                    expected_revision=expected_revision,
                )
            )

    async def policy_reset(self, actor: ActorContext, *, expected_revision: int | None):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.reset_policy(expected_revision=expected_revision)
            )

    async def rotate_trust(self, actor: ActorContext, *, expected_trust_revision: int | None):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(
                self._controller.rotate_local_trust(expected_trust_revision=expected_trust_revision)
            )

    async def reconcile(self, actor: ActorContext):
        OperatorApplication._require_operator(actor)
        with actor.bind():
            return await self._mutation(self._controller.replicate())

    async def internal_state(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                control = await self._controller.authoritative_replication_snapshot()
            except (FleetControlError, ValueError) as exc:
                raise MeshApplicationError("conflict", str(exc)) from exc
            return {"ok": True, "control": control}

    async def internal_mutate(self, actor: ActorContext, *, operation: str, payload: dict):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                control = await self._controller.execute_forwarded(
                    operation, payload, authenticated_peer_id=actor.peer_node_id
                )
            except (FleetControlError, ValueError, KeyError) as exc:
                raise MeshApplicationError("conflict", str(exc)) from exc
            result = {"ok": True, "control": control}
            if operation == "detach-node":
                result["mutation"] = self._controller.mutation_completion(
                    control, force_pending=True
                )
            return result

    async def internal_apply(self, actor: ActorContext, *, control_state: dict):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                state = await self._controller.apply_replica(
                    control_state, source_node_id=actor.peer_node_id
                )
            except (FleetControlError, ValueError) as exc:
                raise MeshApplicationError("conflict", str(exc)) from exc
            return {"ok": True, "control": state}
