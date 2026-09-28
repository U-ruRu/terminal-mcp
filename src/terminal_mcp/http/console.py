from fastapi import APIRouter

from terminal_mcp.api_models import ConsoleSnapshotResponse
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
            service.console_snapshot(
                settings.mode_for("actions"),
                public_base_url=settings.public_base_url,
            ),
        )

    return router
