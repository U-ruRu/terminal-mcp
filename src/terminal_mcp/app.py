import contextlib
import socket

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.routing import Mount

from terminal_mcp.auth.credentials import CredentialManager
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.middleware import AuthMiddleware
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.routes import build_oauth_router
from terminal_mcp.auth.service import AuthService
from terminal_mcp.auth.storage import OAuthStore
from terminal_mcp.config import Settings
from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_execution import (
    CompositePersistentExecutionFence,
    PersistentExecutionFence,
)
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.core.service import TerminalService
from terminal_mcp.fleet.config import build_fleet_config
from terminal_mcp.fleet.replication import FleetReplicationService
from terminal_mcp.fleet.storage import FleetIdentityStore
from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.admin import build_admin_router
from terminal_mcp.http.browser_security import BrowserSecurityMiddleware
from terminal_mcp.http.console import build_console_router
from terminal_mcp.http.console_events import WebSocketTicketStore, build_console_events_router
from terminal_mcp.http.fleet import build_fleet_router
from terminal_mcp.http.pairing import build_pairing_router
from terminal_mcp.http.persistent import build_persistent_router
from terminal_mcp.http.persistent_fleet import build_persistent_fleet_router
from terminal_mcp.http.public import build_public_router
from terminal_mcp.http.rate_limit import RateLimitMiddleware
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.metrics import Metrics
from terminal_mcp.observability import EventLogger
from terminal_mcp.runtime import RuntimeConfigProvider
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter
from terminal_mcp.trace import TraceMiddleware
from terminal_mcp.version import __version__


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.browser_allowed_origins()
    repo = SqliteRepository(
        settings.database_path,
        settings.output_cache_path,
        output_line_max_bytes=settings.output_line_max_bytes,
        output_command_max_bytes=settings.output_command_max_bytes,
        output_target_bytes=settings.output_retention_target_bytes,
        output_max_bytes=settings.output_retention_max_bytes,
        output_max_rows=settings.output_retention_max_rows,
        output_prune_rows=settings.output_retention_prune_rows,
    )
    oauth_store = OAuthStore(settings.database_path)
    auth_foundation = AuthFoundationStore(settings.auth_database_path)
    pairing_store = PairingStore(settings.database_path)
    ws_ticket_store = WebSocketTicketStore(settings.console_ws_ticket_ttl_sec)
    credentials = CredentialManager(settings)
    runtime = RuntimeConfigProvider(settings.runtime_config_path)
    metrics = Metrics(runtime, settings.metrics_host, settings.metrics_port)
    events = EventLogger(settings.log_path, runtime, metrics)
    repo.configure_observability(events, metrics)
    auth_foundation.configure_observability(events, metrics)
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
        post_finish_message_grace_seconds=settings.agent_post_finish_message_grace_sec,
        max_active_agents=settings.max_active_agents,
    )
    fleet_config = build_fleet_config(settings)
    fleet_replication = (
        FleetReplicationService(
            fleet_config,
            FleetIdentityStore(settings.database_path),
            AgentStore(settings.database_path),
            max_session_seconds=settings.agent_max_session_sec,
            events=events,
        )
        if fleet_config
        else None
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
        fleet_replication,
        legacy_agent_admission_enabled=settings.legacy_admission_allowed(),
    )
    persistent_store = PersistentAgentStore(settings.database_path)
    persistent_store.configure_observability(events, metrics)
    persistent_local_fence = PersistentExecutionFence(repo, terminal, service.task_store)
    persistent_fleet = (
        PersistentFleetBridge(fleet_config, persistent_store, repo, terminal, service.task_store)
        if fleet_config and settings.persistent_agents_enabled
        else None
    )
    if persistent_fleet:
        persistent_fleet.execution_fence = persistent_local_fence
        persistent_fence = CompositePersistentExecutionFence(
            persistent_local_fence, persistent_fleet
        )
    else:
        persistent_fence = persistent_local_fence
    persistent_lifecycle = PersistentLifecycleCoordinator(
        persistent_store,
        enabled=settings.persistent_agents_enabled,
        authority_node_id=(
            fleet_config.instance_id
            if fleet_config
            else (settings.fleet_instance_id.strip() or socket.gethostname())
        ),
        session_duration_seconds=settings.persistent_session_duration_sec,
        execution_fence=persistent_fence,
    )
    service.persistent = PersistentBackend(service, persistent_lifecycle, persistent_fleet)
    service.persistent_lifecycle = persistent_lifecycle
    auth = AuthService(settings, oauth_store, credentials)
    mcp = build_mcp(
        service,
        settings.public_base_url,
        settings.mode_for("mcp"),
        persistent_enabled=settings.persistent_agents_enabled,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        events.start()
        await runtime.start()
        await metrics.start()
        events.emit("application_started", outcome="success")
        await repo.initialize()
        await service.reconcile_agent_sessions()
        await oauth_store.initialize()
        await auth_foundation.initialize()
        await pairing_store.initialize()
        if fleet_replication:
            await fleet_replication.start()
        await terminal.start()
        if persistent_fleet:
            await persistent_fleet.start()
        await persistent_lifecycle.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await persistent_lifecycle.stop()
            if persistent_fleet:
                await persistent_fleet.stop()
            await terminal.stop()
            if fleet_replication:
                await fleet_replication.stop()
            events.emit("application_stopped", outcome="success")
            await metrics.stop()
            await runtime.stop()
            events.stop()

    app = FastAPI(title="terminal-mcp", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.oauth_store = oauth_store
    app.state.auth_foundation = auth_foundation
    app.state.pairing_store = pairing_store
    app.state.ws_ticket_store = ws_ticket_store
    app.state.credentials = credentials
    app.state.runtime_config = runtime
    app.state.metrics = metrics
    app.state.events = events
    app.state.event_store = service.event_store
    app.state.fleet_replication = fleet_replication
    app.state.persistent_backend = service.persistent
    app.state.persistent_lifecycle = persistent_lifecycle
    app.state.persistent_fleet = persistent_fleet
    app.include_router(build_public_router())
    if fleet_replication:
        app.include_router(build_fleet_router(fleet_replication))
        if persistent_fleet:
            app.include_router(build_persistent_fleet_router(fleet_replication, persistent_fleet))
    app.include_router(build_pairing_router(settings, auth, pairing_store))
    app.include_router(
        build_console_events_router(
            settings,
            auth,
            pairing_store,
            service,
            service.event_store,
            ws_ticket_store,
        )
    )
    app.include_router(build_oauth_router(settings, auth, oauth_store))
    app.include_router(build_actions_router(service, settings.mode_for("actions")))
    if settings.persistent_agents_enabled:
        app.include_router(build_persistent_router(service))
    app.include_router(build_console_router(service, settings))
    app.include_router(build_admin_router(settings, credentials, oauth_store, terminal, service))
    app.router.routes.append(Mount("/mcp", app=mcp.streamable_http_app()))

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"ok": True, "version": __version__}

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
    app.add_middleware(
        AuthMiddleware, settings=settings, auth_service=auth, pairing_store=pairing_store
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TraceMiddleware)
    app.add_middleware(BrowserSecurityMiddleware, settings=settings)
    return app


app = create_app()
