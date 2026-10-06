from fastapi import APIRouter

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.api_models import ConsoleSnapshotResponse
from terminal_mcp.application import get_application
from terminal_mcp.telemetry import observed


def build_console_router(service, settings):
    router = APIRouter(prefix="/actions/console", tags=["terminal-console"])

    @router.get(
        "/snapshot",
        operation_id="getConsoleSnapshot",
        response_model=ConsoleSnapshotResponse,
        response_model_exclude_none=True,
    )
    async def snapshot():
        return await observed(
            service,
            "rest",
            "console_snapshot",
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "console_snapshot",
                settings.mode_for("actions"),
                public_base_url=settings.public_base_url,
                persistent_policy={
                    "enabled": settings.persistent_agents_enabled,
                    "duration_seconds": settings.persistent_session_duration_sec,
                    "warning_after_seconds": settings.persistent_session_warning_after_sec,
                    "alert_after_seconds": settings.persistent_session_alert_after_sec,
                    "rearm_after_seconds": settings.persistent_session_rearm_after_sec,
                    "manual_rearm": True,
                    "admission_mode": settings.mode_for("mcp"),
                    "legacy_admission_enabled": bool(service.legacy_agent_admission_enabled),
                    "policy_control_supported": True,
                },
            ),
        )

    return router
