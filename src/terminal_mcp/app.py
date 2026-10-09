import contextlib
import json
import socket

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.routing import Mount

from terminal_mcp.application import TerminalApplication
from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.application.managed_identity import (
    ManagedGrantAuthorizer,
    ManagedProviderResolver,
)
from terminal_mcp.application.managed_runtime import ManagedSessionRuntime
from terminal_mcp.application.managed_sessions import ManagedSessionApplication
from terminal_mcp.application.window_recovery import ManagedWindowRecovery
from terminal_mcp.auth.credentials import CredentialManager
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.middleware import AuthMiddleware
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.routes import build_oauth_router
from terminal_mcp.auth.service import AuthService
from terminal_mcp.auth.storage import OAuthStore
from terminal_mcp.config import Settings
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.persistent_execution import (
    CompositePersistentExecutionFence,
    PersistentExecutionFence,
)
from terminal_mcp.core.persistent_fleet import PersistentFleetBridge
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleCoordinator
from terminal_mcp.core.persistent_policy import PersistentPolicyController
from terminal_mcp.core.service import TerminalService
from terminal_mcp.fleet.access_mesh import (
    AccessMeshReplication,
    PinnedAccessMeshPeerAuth,
    build_access_mesh_router,
)
from terminal_mcp.fleet.config import build_fleet_config
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.fleet.projection import FleetProjectionService
from terminal_mcp.fleet.projection_storage import FleetProjectionStore
from terminal_mcp.fleet.protocol import AUTHORITY_CAPABILITIES, SOURCE_CAPABILITIES
from terminal_mcp.fleet.replication import FleetReplicationService
from terminal_mcp.fleet.source import FleetSourceService
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore
from terminal_mcp.fleet.storage import FleetIdentityStore
from terminal_mcp.http.access_mesh import build_access_mesh_operator_router
from terminal_mcp.http.access_mesh_messages import build_access_mesh_message_router
from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.admin import build_admin_router
from terminal_mcp.http.browser_security import BrowserSecurityMiddleware
from terminal_mcp.http.console import build_console_router
from terminal_mcp.http.console_events import WebSocketTicketStore, build_console_events_router
from terminal_mcp.http.console_fleet import build_console_fleet_router
from terminal_mcp.http.fleet import build_fleet_router
from terminal_mcp.http.fleet_control import build_fleet_control_router
from terminal_mcp.http.fleet_v1 import (
    build_fleet_v1_projection_router,
    build_fleet_v1_source_router,
)
from terminal_mcp.http.managed_sessions import build_managed_sessions_router
from terminal_mcp.http.pairing import build_pairing_router
from terminal_mcp.http.persistent import build_persistent_router
from terminal_mcp.http.persistent_fleet import build_persistent_fleet_router
from terminal_mcp.http.provider_identity_fleet import build_provider_identity_fleet_router
from terminal_mcp.http.public import build_public_router
from terminal_mcp.http.rate_limit import RateLimitMiddleware
from terminal_mcp.mcp.access import build_access_mcp
from terminal_mcp.mcp.roles import build_role_mcp
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.metrics import Metrics
from terminal_mcp.observability import EventLogger
from terminal_mcp.runtime import RuntimeConfigProvider
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.application_uow import SqliteApplicationUnitOfWork
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.work_windows import WorkWindowStore
from terminal_mcp.terminal.composition import build_execution
from terminal_mcp.trace import TraceMiddleware
from terminal_mcp.version import __version__


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.browser_allowed_origins()
    execution = build_execution(settings)
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
    fleet_ws_ticket_store = WebSocketTicketStore(settings.console_ws_ticket_ttl_sec)
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
    terminal = CommandScheduler(
        repo,
        execution,
        settings.cancel_grace_sec,
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
    legacy_fleet_replication = (
        fleet_replication if settings.fleet_legacy_replication_enabled else None
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
        legacy_fleet_replication,
        legacy_agent_admission_enabled=settings.legacy_admission_allowed(),
        persistent_agents_enabled=settings.persistent_agents_enabled,
    )
    fleet_node_meta = None
    fleet_source = None
    if settings.fleet_v1_source_enabled:
        if fleet_replication is None:
            raise ValueError("fleet v1 source requires configured fleet peer authentication")
        fleet_node_meta = FleetNodeMetaStore(
            settings.effective_fleet_node_meta_path(),
            fleet_id=settings.fleet_id,
            node_id=settings.effective_fleet_node_id(),
        )
        fleet_node_meta.configure_observability(events, metrics)
        fleet_source = FleetSourceService(
            settings.database_path,
            service.event_store,
            fleet_node_meta,
            runtime_health_provider=terminal.health,
            output_db_path=settings.output_cache_path,
            auth_db_path=settings.auth_database_path,
            metrics=metrics,
            events=events,
        )

    fleet_projection = None
    fleet_projection_service = None
    if settings.fleet_v1_projection_enabled:
        if fleet_config is None or fleet_source is None:
            raise ValueError("fleet v1 projection requires configured source and fleet peers")
        projection_role = settings.effective_fleet_projection_role()
        fleet_projection = FleetProjectionStore(
            settings.effective_fleet_projection_path(),
            fleet_id=settings.fleet_id,
            node_id=settings.effective_fleet_node_id(),
            owner_node_id=settings.fleet_projection_owner_node_id,
            role=projection_role,
        )
        fleet_projection.configure_observability(events, metrics)
        fleet_projection_service = FleetProjectionService(
            fleet_config,
            fleet_projection,
            local_source=fleet_source if projection_role == "owner" else None,
            owner_node_id=settings.fleet_projection_owner_node_id,
            metrics=metrics,
            events=events,
        )

    fleet_control = None
    if settings.fleet_v1_authority_enabled:
        if fleet_config is None:
            raise ValueError("fleet v1 authority requires configured fleet peer authentication")
        fleet_control = FleetControlStore(
            settings.effective_fleet_control_path(),
            fleet_id=settings.fleet_id,
            node_id=settings.effective_fleet_node_id(),
            control_node_id=settings.fleet_control_node_id,
        )
        fleet_control.configure_observability(events, metrics)
    service.fleet_control = fleet_control

    managed_authority_node_id = (
        fleet_config.instance_id
        if fleet_config
        else (settings.fleet_instance_id.strip() or socket.gethostname())
    )
    # WorkWindowStore is a strict PersistentAgentStore superset. Legacy routes
    # keep using the same object while managed capabilities gain schema-20 state.
    persistent_store = WorkWindowStore(
        settings.database_path, authority_node_id=managed_authority_node_id
    )
    persistent_store.configure_observability(events, metrics)
    persistent_local_fence = PersistentExecutionFence(repo, terminal, service.task_store)
    persistent_fleet = (
        PersistentFleetBridge(
            fleet_config,
            persistent_store,
            repo,
            terminal,
            service.task_store,
            permit_ttl_ms=settings.fleet_permit_ttl_ms,
            control_store=fleet_control,
            control_node_id=(
                settings.fleet_control_node_id if settings.fleet_v1_authority_enabled else None
            ),
            access_authority=auth_foundation,
        )
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
        authority_node_id=managed_authority_node_id,
        session_duration_seconds=settings.persistent_session_duration_sec,
        rearm_delay_seconds=settings.persistent_session_rearm_after_sec,
        execution_fence=persistent_fence,
    )
    service.persistent = PersistentBackend(
        service, persistent_lifecycle, persistent_fleet, access_authority=auth_foundation
    )
    service.persistent_lifecycle = persistent_lifecycle
    persistent_policy_controller = PersistentPolicyController(
        settings, service, persistent_lifecycle
    )
    managed_routes = persistent_fleet if fleet_config is not None else None
    managed_identity = ManagedProviderResolver(persistent_store, routes=managed_routes)
    managed_authorizer = ManagedGrantAuthorizer(
        persistent_store, auth_foundation, routes=managed_routes
    )
    managed_sessions = ManagedSessionApplication(
        persistent_store, managed_authorizer, persistent_fence
    )
    managed_recovery = ManagedWindowRecovery(persistent_store, managed_sessions)
    managed_runtime = ManagedSessionRuntime(
        managed_identity,
        managed_sessions,
        managed_recovery,
        enabled=settings.persistent_agents_enabled,
    )

    managed_fleet_control = (
        ManagedFleetControl(
            fleet_control,
            fleet_config,
            persistent_policy_controller,
            public_base_url=settings.public_base_url,
            runtime_targets=(
                fleet_replication,
                fleet_projection_service,
                persistent_fleet,
            ),
        )
        if fleet_control and fleet_config and fleet_replication
        else None
    )
    access_mesh = None
    access_mesh_replication = None
    access_mesh_messages = None
    if settings.access_mesh_enabled:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        mesh_store = AccessMeshStore(
            settings.database_path,
            local_node_id=settings.fleet_instance_id,
            trusted_issuers=frozenset(
                [settings.fleet_instance_id, *json.loads(settings.access_mesh_peers_json)]
            ),
            proof_key=settings.access_mesh_proof_key.encode(),
        )
        access_mesh = AccessMeshApplication(
            mesh_store,
            task_store=service.task_store,
            execution_fence=persistent_local_fence,
            legacy_enabled=settings.access_mesh_legacy_enabled,
            defaults=SlotPolicy(
                settings.access_mesh_duration_sec,
                settings.access_mesh_cooldown_sec,
                True,
                settings.access_mesh_warning_sec,
                settings.access_mesh_draining_sec,
            ),
        )
        service.access_mesh = access_mesh
        persistent_lifecycle.access_mesh = access_mesh
        if fleet_config and fleet_replication:
            access_mesh_replication = AccessMeshReplication(access_mesh, fleet_config)
            access_mesh.replication = access_mesh_replication
            service.access_mesh_replication = access_mesh_replication
        access_mesh_messages = AccessMeshMessaging(
            access_mesh,
            agent_store=service.agent_coordinator.store,
            replication=access_mesh_replication,
        )
        service.access_mesh_messages = access_mesh_messages

    application = TerminalApplication(
        service,
        auth_mode=settings.mode_for("mcp"),
        policy_controller=persistent_policy_controller,
        unit_of_work=SqliteApplicationUnitOfWork(settings.database_path),
        replication=fleet_replication,
        fleet_source=fleet_source,
        fleet_projection=fleet_projection,
        projection_service=fleet_projection_service,
        fleet_control=managed_fleet_control,
        managed_identity=managed_identity,
        managed_sessions=managed_sessions,
    )
    service.application = application
    auth = AuthService(settings, oauth_store, credentials)
    mcp = build_mcp(
        application,
        settings.public_base_url,
        settings.mode_for("mcp"),
        persistent_enabled=settings.persistent_agents_enabled,
    )

    executor_mcp = build_role_mcp(
        application, "executor", settings.public_base_url, settings.mode_for("mcp")
    )
    coordinator_mcp = build_role_mcp(
        application, "coordinator", settings.public_base_url, settings.mode_for("mcp")
    )

    access_mcp = (
        build_access_mcp(application, public_base_url=settings.public_base_url)
        if access_mesh
        else None
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        events.start()
        await runtime.start()
        await metrics.start()
        events.emit("application_started", outcome="success")
        await repo.initialize(
            preserve_active_commands=bool(getattr(execution, "reconnectable", False))
        )
        if fleet_node_meta:
            await fleet_node_meta.initialize()
        if fleet_projection:
            await fleet_projection.initialize()
        if fleet_control:
            await fleet_control.initialize()
            local_capabilities = set(AUTHORITY_CAPABILITIES)
            if fleet_source:
                local_capabilities.update(SOURCE_CAPABILITIES)
            await fleet_control.register_member(
                fleet_config.instance_id,
                local_capabilities,
            )
            for peer in fleet_config.peers:
                await fleet_control.register_member(
                    peer.instance_id,
                    (),
                )
            if fleet_projection:
                await fleet_control.ensure_projection_topology(
                    settings.fleet_projection_owner_node_id,
                    settings.fleet_projection_follower_node_id,
                )
            if managed_fleet_control:
                await managed_fleet_control.reconcile_local()
        await service.reconcile_agent_sessions()
        await oauth_store.initialize()
        await auth_foundation.initialize()
        await pairing_store.initialize()
        if legacy_fleet_replication:
            await legacy_fleet_replication.start()
        await terminal.start()
        if fleet_projection_service:
            await fleet_projection_service.start()
        if persistent_fleet:
            await persistent_fleet.start()
        if managed_fleet_control:
            await managed_fleet_control.start()
        await persistent_lifecycle.start()
        await managed_runtime.start()
        if access_mesh:
            await access_mesh.start()
        if access_mesh_messages:
            await access_mesh_messages.start()
        if access_mesh_replication:
            await access_mesh_replication.start()
        try:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(mcp.session_manager.run())
                await stack.enter_async_context(executor_mcp.session_manager.run())
                await stack.enter_async_context(coordinator_mcp.session_manager.run())
                if access_mcp:
                    await stack.enter_async_context(access_mcp.session_manager.run())
                yield
        finally:
            if access_mesh_messages:
                await access_mesh_messages.stop()
            if access_mesh_replication:
                await access_mesh_replication.stop()
            if access_mesh:
                await access_mesh.stop()
            await managed_runtime.stop()
            await persistent_lifecycle.stop()
            if managed_fleet_control:
                await managed_fleet_control.stop()
            if persistent_fleet:
                await persistent_fleet.stop()
            if fleet_projection_service:
                await fleet_projection_service.stop()
            await terminal.stop()
            if legacy_fleet_replication:
                await legacy_fleet_replication.stop()
            events.emit("application_stopped", outcome="success")
            await metrics.stop()
            await runtime.stop()
            events.stop()

    app = FastAPI(title="terminal-mcp", version=__version__, lifespan=lifespan)
    app.state.access_mesh = access_mesh
    app.state.access_mesh_messages = access_mesh_messages
    app.state.access_mesh_replication = access_mesh_replication
    app.state.access_mcp = access_mcp
    app.state.executor_mcp = executor_mcp
    app.state.coordinator_mcp = coordinator_mcp
    app.state.settings = settings
    app.state.service = service
    app.state.application = application
    app.state.oauth_store = oauth_store
    app.state.auth_foundation = auth_foundation
    app.state.pairing_store = pairing_store
    app.state.ws_ticket_store = ws_ticket_store
    app.state.fleet_ws_ticket_store = fleet_ws_ticket_store
    app.state.credentials = credentials
    app.state.runtime_config = runtime
    app.state.metrics = metrics
    app.state.events = events
    app.state.event_store = service.event_store
    app.state.fleet_replication = fleet_replication
    app.state.legacy_fleet_replication = legacy_fleet_replication
    app.state.fleet_node_meta = fleet_node_meta
    app.state.fleet_source = fleet_source
    app.state.fleet_control = fleet_control
    app.state.managed_fleet_control = managed_fleet_control
    app.state.fleet_projection = fleet_projection
    app.state.fleet_projection_service = fleet_projection_service
    app.state.persistent_backend = service.persistent
    app.state.managed_identity = managed_identity
    app.state.managed_sessions = managed_sessions
    app.state.managed_recovery = managed_recovery
    app.state.managed_runtime = managed_runtime
    app.state.persistent_lifecycle = persistent_lifecycle
    app.state.persistent_policy_controller = persistent_policy_controller
    app.state.persistent_fleet = persistent_fleet
    app.include_router(build_public_router())
    if access_mesh:
        app.include_router(build_access_mesh_operator_router(application))
    if access_mesh and fleet_replication:
        # Managed Fleet Control changes fleet_replication.config in place at
        # startup. Access Mesh outbound uses the bootstrap config; inbound
        # authentication MUST use that same pinned trust/token material.
        mesh_peer_auth = PinnedAccessMeshPeerAuth(fleet_config)
        app.include_router(build_access_mesh_router(access_mesh, mesh_peer_auth))
        app.include_router(
            build_access_mesh_message_router(access_mesh_messages, mesh_peer_auth)
        )
    if fleet_replication:
        if settings.fleet_legacy_replication_enabled:
            app.include_router(
                build_fleet_router(fleet_replication, application=application.replication)
            )
        if fleet_source:
            app.include_router(
                build_fleet_v1_source_router(
                    fleet_source, fleet_replication, application=application.fleet_source
                )
            )
        if fleet_projection:
            app.include_router(
                build_fleet_v1_projection_router(
                    fleet_projection,
                    fleet_replication,
                    fleet_projection_service,
                    application=application.fleet_projection,
                )
            )
        if persistent_fleet:
            app.include_router(
                build_persistent_fleet_router(
                    fleet_replication,
                    persistent_fleet,
                    service.persistent,
                    application=application.mesh,
                )
            )
            app.include_router(
                build_provider_identity_fleet_router(
                    fleet_replication, persistent_fleet, application=application.mesh
                )
            )
        if managed_fleet_control:
            app.include_router(
                build_fleet_control_router(
                    managed_fleet_control, fleet_replication, application=application.fleet_control
                )
            )
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
    if settings.fleet_v1_public_enabled and fleet_projection:
        app.include_router(
            build_console_fleet_router(
                settings,
                auth,
                pairing_store,
                fleet_projection,
                fleet_ws_ticket_store,
                fleet_projection_service,
            )
        )
    app.include_router(build_oauth_router(settings, auth, oauth_store))
    app.include_router(build_actions_router(service, settings.mode_for("actions")))
    if settings.persistent_agents_enabled:
        app.include_router(build_persistent_router(service, persistent_policy_controller))
        app.include_router(build_managed_sessions_router(service))
    app.include_router(build_console_router(service, settings))
    app.include_router(build_admin_router(settings, credentials, oauth_store, terminal, service))
    app.router.routes.append(
        Mount("/terminal-mcp/executor/v1/mcp", app=executor_mcp.streamable_http_app())
    )
    app.router.routes.append(
        Mount("/terminal-mcp/coordinator/v1/mcp", app=coordinator_mcp.streamable_http_app())
    )
    if access_mcp:
        app.router.routes.append(
            Mount("/terminal-mcp/access/v1/mcp", app=access_mcp.streamable_http_app())
        )
    app.router.routes.append(Mount("/mcp", app=mcp.streamable_http_app()))

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"ok": True, "version": __version__}

    from terminal_mcp.operation_metadata import (
        action_is_consequential,
        openapi_action_matrix,
        openapi_effects,
    )

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
                    operation["x-terminal-mcp-effects"] = openapi_effects(
                        operation.get("operationId", "")
                    )
                    operation["x-terminal-mcp-action-matrix"] = openapi_action_matrix(
                        operation.get("operationId", "")
                    )
                    operation["x-openai-isConsequential"] = action_is_consequential(
                        operation.get("operationId", "")
                    )
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
