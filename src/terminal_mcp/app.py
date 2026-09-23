import contextlib

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.routing import Mount

from terminal_mcp.auth.credentials import CredentialManager
from terminal_mcp.auth.middleware import AuthMiddleware
from terminal_mcp.auth.routes import build_oauth_router
from terminal_mcp.auth.service import AuthService
from terminal_mcp.auth.storage import OAuthStore
from terminal_mcp.config import Settings
from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.service import TerminalService
from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.admin import build_admin_router
from terminal_mcp.http.public import build_public_router
from terminal_mcp.http.rate_limit import RateLimitMiddleware
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.metrics import Metrics
from terminal_mcp.observability import EventLogger
from terminal_mcp.runtime import RuntimeConfigProvider
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter
from terminal_mcp.trace import TraceMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repo = SqliteRepository(
        settings.database_path,
        settings.output_cache_path,
        output_line_max_bytes=settings.output_line_max_bytes,
        output_command_max_bytes=settings.output_command_max_bytes,
        output_target_bytes=settings.output_retention_target_bytes,
        output_max_bytes=settings.output_retention_max_bytes,
        output_max_rows=settings.output_retention_max_rows,
    )
    oauth_store = OAuthStore(settings.database_path)
    credentials = CredentialManager(settings)
    runtime = RuntimeConfigProvider(settings.runtime_config_path)
    metrics = Metrics(runtime, settings.metrics_host, settings.metrics_port)
    events = EventLogger(settings.log_path, runtime, metrics)
    repo.configure_observability(events, metrics)
    runtime.warning_callback = lambda error: events.emit(
        "runtime_config_invalid", level="WARNING", outcome="invalid", error=error
    )
    runtime.reload_callback = lambda config: events.emit(
        "runtime_config_reloaded", outcome="success"
    )
    terminal = LinuxTerminalAdapter(
        repo,
        settings.shell,
        settings.cwd,
        settings.cancel_grace_sec,
        settings.terminal_user,
        settings.queue_workers,
        settings.queue_reconcile_sec,
    )
    agent_policy = AgentPolicy(
        idle_ttl_seconds=settings.agent_idle_ttl_sec,
        intent_ttl_seconds=settings.agent_intent_ttl_sec,
        max_session_seconds=settings.agent_max_session_sec,
        session_warning_after_seconds=settings.agent_session_warning_after_sec,
        session_alert_enabled=settings.agent_session_alert_enabled,
        session_alert_after_seconds=settings.agent_session_alert_after_sec,
        session_alert_repeat_seconds=settings.agent_session_alert_repeat_sec,
        session_alert_message=settings.agent_session_alert_message,
        event_window_seconds=settings.agent_event_window_sec,
        command_preview_chars=settings.agent_command_preview_chars,
        history_default_minutes=settings.agent_history_default_minutes,
        message_reminder_seconds=settings.message_reminder_sec,
        message_reminder_calls=settings.message_reminder_calls,
        max_active_agents=settings.max_active_agents,
    )
    service = TerminalService(
        repo,
        terminal,
        settings.max_read_lines,
        settings.auth_mode,
        settings.health_command,
        runtime,
        events,
        metrics,
        agent_policy,
    )
    auth = AuthService(settings, oauth_store, credentials)
    mcp = build_mcp(service, settings.public_base_url, settings.mode_for("mcp"))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        events.start()
        await runtime.start()
        await metrics.start()
        events.emit("application_started", outcome="success")
        await repo.initialize()
        await oauth_store.initialize()
        await terminal.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await terminal.stop()
            events.emit("application_stopped", outcome="success")
            await metrics.stop()
            await runtime.stop()
            events.stop()

    app = FastAPI(title="terminal-mcp", version="0.9.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.oauth_store = oauth_store
    app.state.credentials = credentials
    app.state.runtime_config = runtime
    app.state.metrics = metrics
    app.state.events = events
    app.include_router(build_public_router())
    app.include_router(build_oauth_router(settings, auth, oauth_store))
    app.include_router(build_actions_router(service, settings.mode_for("actions")))
    app.include_router(build_admin_router(settings, credentials, oauth_store, terminal, service))
    app.router.routes.append(Mount("/mcp", app=mcp.streamable_http_app()))

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"ok": True}

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema["servers"] = [{"url": settings.public_base_url}]
        schema["paths"] = {
            path: item
            for path, item in schema.get("paths", {}).items()
            if path.startswith("/actions/")
        }
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "API token",
        }
        for item in schema["paths"].values():
            for method, operation in item.items():
                if method.lower() in {"get", "post", "put", "patch", "delete"}:
                    operation["security"] = [{"BearerAuth": []}]
                    operation["x-openai-isConsequential"] = False
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    app.add_middleware(AuthMiddleware, settings=settings, auth_service=auth)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TraceMiddleware)
    return app


app = create_app()
