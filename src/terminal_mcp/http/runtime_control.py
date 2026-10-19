"""Paired Console maintenance of the local installed node."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from terminal_mcp.deployment.runtime_control import RuntimeControlError, build_runtime_control


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["terminal", "tunnel"]
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")


def build_runtime_router(settings, *, control=None):
    router = APIRouter(prefix="/actions/fleet/control/runtime", tags=["node-runtime"])
    control = control if control is not None else build_runtime_control(settings)

    @router.get("", operation_id="getNodeRuntime")
    async def state():
        if control is None:
            return {
                "ok": True,
                "platform": "other",
                "supported": False,
                "restart_targets": [],
                "services": {},
                "operation": None,
            }
        return await asyncio.to_thread(control.snapshot)

    @router.get("/operations/{request_id}", operation_id="getNodeRuntimeOperation")
    async def operation(request_id: str):
        if control is None:
            raise HTTPException(404, detail="runtime_unavailable")
        try:
            result = await asyncio.to_thread(control.operation, request_id)
        except RuntimeControlError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(404, detail="runtime_operation_missing")
        return {"ok": True, "operation": result}

    @router.post("/restart", operation_id="restartNodeRuntime", status_code=202)
    async def restart(body: RestartRequest):
        if control is None:
            raise HTTPException(409, detail="runtime_unavailable")
        try:
            result = await asyncio.to_thread(control.submit, body.target, body.request_id)
        except RuntimeControlError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        return {"ok": True, "operation": result}

    return router
