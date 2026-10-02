from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.fleet.control_storage import FleetControlError


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdoptRequest(StrictRequest):
    mesh_id: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str = Field(default="Fleet", min_length=1, max_length=120)


class RenameRequest(StrictRequest):
    mesh_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    expected_topology_revision: int | None = Field(default=None, ge=0)


class DeleteMeshRequest(StrictRequest):
    mesh_id: str = Field(min_length=1, max_length=64)
    expected_topology_revision: int | None = Field(default=None, ge=0)


class NodeUpsertRequest(StrictRequest):
    node_id: str = Field(min_length=1, max_length=64)
    mesh_id: str = Field(min_length=1, max_length=64)
    origin: str | None = Field(default=None, max_length=500)
    public_key: str | None = Field(default=None, max_length=200)
    auth_token: str | None = Field(default=None, min_length=1, max_length=500)
    expected_topology_revision: int | None = Field(default=None, ge=0)


class NodeDetachRequest(StrictRequest):
    node_id: str = Field(min_length=1, max_length=64)
    expected_topology_revision: int | None = Field(default=None, ge=0)


class NodeMoveRequest(StrictRequest):
    node_id: str = Field(min_length=1, max_length=64)
    target_mesh_id: str = Field(min_length=1, max_length=64)
    expected_topology_revision: int | None = Field(default=None, ge=0)


class AccessPolicyRequest(StrictRequest):
    duration_seconds: int = Field(ge=1)
    warning_after_seconds: int = Field(ge=1)
    alert_after_seconds: int = Field(ge=1)
    rearm_after_seconds: int = Field(ge=1)
    legacy_admission_enabled: bool
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_thresholds(self):
        if not (self.warning_after_seconds < self.alert_after_seconds < self.duration_seconds):
            raise ValueError("policy timing must satisfy warning < alert < duration")
        return self


class ResetPolicyRequest(StrictRequest):
    expected_revision: int | None = Field(default=None, ge=1)


class RotateTrustRequest(StrictRequest):
    expected_trust_revision: int | None = Field(default=None, ge=1)


class InternalApplyRequest(StrictRequest):
    state: dict


class InternalMutationRequest(StrictRequest):
    payload: dict = Field(default_factory=dict)


def build_fleet_control_router(controller, replication) -> APIRouter:
    router = APIRouter(tags=["fleet-control"])

    async def mutation(call):
        try:
            return {"ok": True, "control": await call}
        except (FleetControlError, ValueError) as exc:
            return {"ok": False, "code": str(exc), "error": str(exc)}

    @router.get("/actions/fleet/control", operation_id="getManagedFleetControl")
    async def state():
        return {"ok": True, "control": await controller.snapshot()}

    @router.get("/actions/fleet/control/enrollment", include_in_schema=False)
    async def enrollment():
        return {"ok": True, "enrollment": await controller.enrollment_descriptor()}

    @router.post("/actions/fleet/control/adopt", operation_id="adoptManagedFleetControl")
    async def adopt(body: AdoptRequest):
        return await mutation(
            controller.adopt(mesh_id=body.mesh_id, display_name=body.display_name)
        )

    @router.post("/actions/fleet/control/mesh/delete", operation_id="deleteManagedMesh")
    async def delete_mesh(body: DeleteMeshRequest):
        return await mutation(
            controller.delete_mesh(
                mesh_id=body.mesh_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        )

    @router.post("/actions/fleet/control/mesh/rename", operation_id="renameManagedMesh")
    async def rename(body: RenameRequest):
        return await mutation(
            controller.rename(
                body.display_name,
                mesh_id=body.mesh_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        )

    @router.post("/actions/fleet/control/nodes/upsert", operation_id="upsertManagedFleetNode")
    async def upsert_node(body: NodeUpsertRequest):
        return await mutation(
            controller.upsert_node(
                node_id=body.node_id,
                mesh_id=body.mesh_id,
                origin=body.origin,
                public_key=body.public_key,
                auth_token=body.auth_token,
                expected_topology_revision=body.expected_topology_revision,
            )
        )

    @router.post("/actions/fleet/control/nodes/detach", operation_id="detachManagedFleetNode")
    async def detach_node(body: NodeDetachRequest):
        return await mutation(
            controller.detach_node(
                body.node_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        )

    @router.post("/actions/fleet/control/nodes/move", operation_id="moveManagedFleetNode")
    async def move_node(body: NodeMoveRequest):
        return await mutation(
            controller.move_node(
                body.node_id,
                body.target_mesh_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        )

    @router.post("/actions/fleet/control/policy", operation_id="updateManagedAccessPolicy")
    async def policy(body: AccessPolicyRequest):
        return await mutation(
            controller.update_policy(
                duration_seconds=body.duration_seconds,
                warning_after_seconds=body.warning_after_seconds,
                alert_after_seconds=body.alert_after_seconds,
                rearm_after_seconds=body.rearm_after_seconds,
                legacy_admission_enabled=body.legacy_admission_enabled,
                expected_revision=body.expected_revision,
            )
        )

    @router.post("/actions/fleet/control/policy/reset", operation_id="resetManagedAccessPolicy")
    async def policy_reset(body: ResetPolicyRequest):
        return await mutation(controller.reset_policy(expected_revision=body.expected_revision))

    @router.post("/actions/fleet/control/trust/rotate", operation_id="rotateManagedFleetTrust")
    async def rotate_trust(body: RotateTrustRequest):
        return await mutation(
            controller.rotate_local_trust(
                expected_trust_revision=body.expected_trust_revision,
            )
        )

    @router.post("/actions/fleet/control/reconcile", operation_id="reconcileManagedFleetControl")
    async def reconcile():
        return await mutation(controller.replicate())

    async def authenticate(
        peer_id: str,
        authorization: str,
        *,
        first_apply_control_node_id: str | None = None,
    ):
        peer = await controller.authenticate_management_peer(
            peer_id,
            authorization,
            first_apply_control_node_id=first_apply_control_node_id,
        )
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return peer

    @router.post("/internal/fleet/control/mutate/{operation}", include_in_schema=False)
    async def internal_mutate(
        operation: str,
        body: InternalMutationRequest,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        await authenticate(x_terminal_mcp_peer, authorization)
        try:
            control = await controller.execute_forwarded(
                operation,
                body.payload,
                authenticated_peer_id=x_terminal_mcp_peer,
            )
        except (FleetControlError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "control": control}

    @router.post("/internal/fleet/control/apply", include_in_schema=False)
    async def internal_apply(
        body: InternalApplyRequest,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        await authenticate(
            x_terminal_mcp_peer,
            authorization,
            first_apply_control_node_id=str(body.state.get("control_node_id") or "") or None,
        )
        try:
            state = await controller.apply_replica(
                body.state,
                source_node_id=x_terminal_mcp_peer,
            )
        except (FleetControlError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "control": state}

    return router
