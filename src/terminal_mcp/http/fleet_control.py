from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.fleet_control import FleetControlApplication
from terminal_mcp.application.mesh import MeshApplicationError
from terminal_mcp.core.persistent_admission import current_admission_context

_ERROR_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
}


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdoptRequest(StrictRequest):
    mesh_id: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str = Field(default="Fleet", min_length=1, max_length=120)
    control_node_id: str | None = Field(default=None, min_length=1, max_length=64)


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


def build_fleet_control_router(controller, replication, *, application=None) -> APIRouter:
    router = APIRouter(tags=["fleet-control"])
    target = application if application is not None else FleetControlApplication(controller)

    def operator_actor() -> ActorContext:
        return ActorContext.from_admission(
            current_admission_context(),
            transport="http",
            endpoint_role="operator",
            node_id=str(getattr(getattr(controller, "config", None), "instance_id", "") or ""),
        )

    async def authenticate(
        peer_id: str,
        authorization: str,
        *,
        first_apply_control_node_id=None,
        allow_detached_peer: bool = False,
    ):
        verified_peer_id = await controller.authenticate_management_peer(
            peer_id,
            authorization,
            first_apply_control_node_id=first_apply_control_node_id,
            allow_detached_peer=allow_detached_peer,
        )
        if verified_peer_id is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return ActorContext(
            transport="mesh",
            endpoint_role="mesh",
            node_id=str(getattr(getattr(controller, "config", None), "instance_id", "") or ""),
            peer_node_id=verified_peer_id,
        )

    @router.get("/actions/fleet/control", operation_id="getManagedFleetControl")
    async def state():
        actor = operator_actor()
        try:
            return await target.state(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/actions/fleet/control/enrollment", include_in_schema=False)
    async def enrollment():
        actor = operator_actor()
        try:
            return await target.enrollment(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/adopt", operation_id="adoptManagedFleetControl")
    async def adopt(body: AdoptRequest):
        actor = operator_actor()
        try:
            return await target.adopt(
                actor,
                mesh_id=body.mesh_id,
                display_name=body.display_name,
                control_node_id=body.control_node_id,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/mesh/delete", operation_id="deleteManagedMesh")
    async def delete_mesh(body: DeleteMeshRequest):
        actor = operator_actor()
        try:
            return await target.delete_mesh(
                actor,
                mesh_id=body.mesh_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/mesh/rename", operation_id="renameManagedMesh")
    async def rename(body: RenameRequest):
        actor = operator_actor()
        try:
            return await target.rename(
                actor,
                mesh_id=body.mesh_id,
                display_name=body.display_name,
                expected_topology_revision=body.expected_topology_revision,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/nodes/upsert", operation_id="upsertManagedFleetNode")
    async def upsert_node(body: NodeUpsertRequest):
        actor = operator_actor()
        try:
            return await target.upsert_node(
                actor,
                node_id=body.node_id,
                mesh_id=body.mesh_id,
                origin=body.origin,
                public_key=body.public_key,
                auth_token=body.auth_token,
                expected_topology_revision=body.expected_topology_revision,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/nodes/detach", operation_id="detachManagedFleetNode")
    async def detach_node(body: NodeDetachRequest):
        actor = operator_actor()
        try:
            return await target.detach_node(
                actor,
                node_id=body.node_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/nodes/move", operation_id="moveManagedFleetNode")
    async def move_node(body: NodeMoveRequest):
        actor = operator_actor()
        try:
            return await target.move_node(
                actor,
                node_id=body.node_id,
                target_mesh_id=body.target_mesh_id,
                expected_topology_revision=body.expected_topology_revision,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/policy", operation_id="updateManagedAccessPolicy")
    async def policy(body: AccessPolicyRequest):
        actor = operator_actor()
        try:
            return await target.policy(
                actor,
                duration_seconds=body.duration_seconds,
                warning_after_seconds=body.warning_after_seconds,
                alert_after_seconds=body.alert_after_seconds,
                rearm_after_seconds=body.rearm_after_seconds,
                legacy_admission_enabled=body.legacy_admission_enabled,
                expected_revision=body.expected_revision,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/policy/reset", operation_id="resetManagedAccessPolicy")
    async def policy_reset(body: ResetPolicyRequest):
        actor = operator_actor()
        try:
            return await target.policy_reset(actor, expected_revision=body.expected_revision)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/trust/rotate", operation_id="rotateManagedFleetTrust")
    async def rotate_trust(body: RotateTrustRequest):
        actor = operator_actor()
        try:
            return await target.rotate_trust(
                actor, expected_trust_revision=body.expected_trust_revision
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/actions/fleet/control/reconcile", operation_id="reconcileManagedFleetControl")
    async def reconcile():
        actor = operator_actor()
        try:
            return await target.reconcile(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/control/state", include_in_schema=False)
    async def internal_state(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = await authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.internal_state(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/control/mutate/{operation}", include_in_schema=False)
    async def internal_mutate(
        operation: str,
        body: InternalMutationRequest,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = await authenticate(
            x_terminal_mcp_peer,
            authorization,
            allow_detached_peer=operation == "release-node",
        )
        try:
            return await target.internal_mutate(actor, operation=operation, payload=body.payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/control/apply", include_in_schema=False)
    async def internal_apply(
        body: InternalApplyRequest,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = await authenticate(
            x_terminal_mcp_peer,
            authorization,
            first_apply_control_node_id=str(body.state.get("control_node_id") or "") or None,
        )
        try:
            return await target.internal_apply(actor, control_state=body.state)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    return router
