import asyncio
import contextlib
import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.routing import Mount

from terminal_mcp.application import TerminalApplication
from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.auth.credentials import CredentialManager
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import (
    assert_identity_isolated,
    initialize_fresh_identity,
)
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
    PersistentExecutionFence,
)
from terminal_mcp.core.persistent_policy import PersistentPolicyController
from terminal_mcp.core.service import TerminalService
from terminal_mcp.core.session_execution import SessionExecutionCoordinator
from terminal_mcp.fleet.access_mesh import (
    AccessMeshReplication,
    PinnedAccessMeshPeerAuth,
    build_access_mesh_router,
)
from terminal_mcp.fleet.config import build_fleet_config
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.fleet.event_mesh import LifecycleEventMesh, build_lifecycle_event_router
from terminal_mcp.fleet.message_event_mesh import build_message_event_router
from terminal_mcp.fleet.namespace import finish_namespace_transition
from terminal_mcp.fleet.peer_auth import MeshPeerTransport
from terminal_mcp.fleet.projection import FleetProjectionService
from terminal_mcp.fleet.projection_storage import FleetProjectionStore
from terminal_mcp.fleet.protocol import AUTHORITY_CAPABILITIES, SOURCE_CAPABILITIES
from terminal_mcp.fleet.source import FleetSourceService
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore
from terminal_mcp.http.access_mesh import build_access_mesh_operator_router
from terminal_mcp.http.access_mesh_messages import build_access_mesh_message_router
from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.admin import build_admin_router
from terminal_mcp.http.browser_security import BrowserSecurityMiddleware
from terminal_mcp.http.console import build_console_router
from terminal_mcp.http.console_events import WebSocketTicketStore, build_console_events_router
from terminal_mcp.http.console_fleet import build_console_fleet_router
from terminal_mcp.http.fleet_control import build_fleet_control_router
from terminal_mcp.http.fleet_v1 import (
    build_fleet_v1_projection_router,
    build_fleet_v1_source_router,
)
from terminal_mcp.http.mesh_vpn_control import build_mesh_vpn_control_router
from terminal_mcp.http.pairing import build_pairing_router
from terminal_mcp.http.public import build_public_router
from terminal_mcp.http.rate_limit import RateLimitMiddleware
from terminal_mcp.http.runtime_control import build_runtime_router
from terminal_mcp.mcp.access import build_access_mcp
from terminal_mcp.mcp.event_diagnostics import MCPEventIngressProbe
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.mcp.message_binding import MeshMessageSubscriberResolver
from terminal_mcp.mcp.roles import build_role_mcp
from terminal_mcp.metrics import Metrics
from terminal_mcp.node_identity import ensure_node_identity
from terminal_mcp.observability import EventLogger
from terminal_mcp.runtime import RuntimeConfigProvider
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.application_uow import SqliteApplicationUnitOfWork
from terminal_mcp.storage.mesh_runtime_migration import (
    assert_runtime_migration_safe,
    migrate_unified_runtime,
    prior_persistent_defaults,
)
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.composition import build_execution
from terminal_mcp.trace import TraceMiddleware
from terminal_mcp.version import __release__, __version__


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.browser_allowed_origins()
    existing_runtime = settings.database_path.exists()
    fresh_identity_install = assert_identity_isolated(
        settings.database_path, settings.auth_database_path
    )
    assert_runtime_migration_safe(settings.database_path)
    migrated_defaults = prior_persistent_defaults(settings)
    ensure_node_identity(settings)
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
    oauth_store = OAuthStore(settings.auth_database_path)
    auth_foundation = AuthFoundationStore(settings.auth_database_path)
    pairing_store = PairingStore(settings.auth_database_path)
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
        command_preview_chars=settings.agent_command_preview_chars,
        history_default_minutes=settings.agent_history_default_minutes,
        max_active_agents=settings.max_active_agents,
    )
    fleet_config = build_fleet_config(settings)
    fleet_replication = MeshPeerTransport(fleet_config) if fleet_config else None
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
    fleet_node_meta = None
    fleet_source = None
    if fleet_config is not None:
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
            local_source=fleet_source,
            owner_node_id=settings.fleet_projection_owner_node_id,
            metrics=metrics,
            events=events,
        )

    fleet_control = None
    if fleet_config is not None:
        fleet_control = FleetControlStore(
            settings.effective_fleet_control_path(),
            fleet_id=settings.fleet_id,
            node_id=settings.effective_fleet_node_id(),
            control_node_id=settings.fleet_control_node_id,
        )
        fleet_control.configure_observability(events, metrics)
    service.fleet_control = fleet_control

    managed_authority_node_id = (
        fleet_config.instance_id if fleet_config else settings.fleet_instance_id
    )
    persistent_store = PersistentAgentStore(settings.database_path)
    persistent_store.configure_observability(events, metrics)
    persistent_local_fence = PersistentExecutionFence(repo, terminal, service.task_store)
    persistent_lifecycle = SessionExecutionCoordinator(
        persistent_store,
        session_duration_seconds=settings.persistent_session_duration_sec,
        rearm_delay_seconds=settings.persistent_session_rearm_after_sec,
    )
    service.persistent = PersistentBackend(service, persistent_lifecycle)
    service.persistent_lifecycle = persistent_lifecycle
    persistent_policy_controller = PersistentPolicyController(
        settings, service, persistent_lifecycle
    )

    async def sync_namespace(namespace: str | None = None) -> None:
        guard = (
            fleet_projection_service.namespace_transition()
            if fleet_projection_service is not None
            else contextlib.nullcontext()
        )
        async with guard as mark_applied:
            accepted = await asyncio.to_thread(finish_namespace_transition, settings)
            if accepted is None:
                return
            if namespace is not None and accepted != namespace:
                raise RuntimeError("namespace_transition_identity_mismatch")
            for metadata in (fleet_node_meta, fleet_projection, fleet_control):
                if metadata is not None:
                    metadata.fleet_id = accepted
            if mark_applied is not None:
                mark_applied()

    managed_fleet_control = (
        ManagedFleetControl(
            fleet_control,
            fleet_config,
            persistent_policy_controller,
            public_base_url=settings.public_base_url,
            namespace_sync=sync_namespace,
            runtime_targets=(
                fleet_replication,
                fleet_projection_service,
            ),
        )
        if fleet_control and fleet_config and fleet_replication
        else None
    )
    access_mesh = None
    access_mesh_replication = None
    access_mesh_messages = None
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    native_defaults = migrated_defaults or SlotPolicy(
        settings.access_mesh_duration_sec,
        settings.access_mesh_cooldown_sec,
        True,
        settings.access_mesh_warning_sec,
        settings.access_mesh_draining_sec,
    )
    mesh_store = AccessMeshStore(
        settings.database_path,
        upgrade_defaults=native_defaults,
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
        defaults=native_defaults,
    )
    service.access_mesh = access_mesh
    if managed_fleet_control is not None:
        managed_fleet_control.native_access = access_mesh
    if fleet_config and fleet_replication:
        access_mesh_replication = AccessMeshReplication(access_mesh, fleet_config)
        access_mesh.replication = access_mesh_replication
        service.access_mesh_replication = access_mesh_replication
        if managed_fleet_control is not None:
            managed_fleet_control.runtime_targets += (access_mesh_replication,)
    access_mesh_messages = AccessMeshMessaging(
        access_mesh,
        agent_store=service.agent_store,
        replication=access_mesh_replication,
    )
    service.access_mesh_messages = access_mesh_messages
    access_mesh.messages = access_mesh_messages

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
    )
    service.application = application
    auth = AuthService(settings, oauth_store, credentials)
    executor_mcp = build_role_mcp(
        application, "executor", settings.public_base_url, settings.mode_for("mcp")
    )
    coordinator_mcp = build_role_mcp(
        application, "coordinator", settings.public_base_url, settings.mode_for("mcp")
    )

    # Construct Access after the Events store so get_events can share its
    # durable agent-scoped inbox rather than introducing a second queue.
    access_mcp = None

    async def mcp_events_access_checker(item):
        """Recheck the same authorization source on every callback delivery."""
        credential = item.get("credential_id") or ""
        if item.get("auth_mode") == "oauth" and credential.startswith("oauth:"):
            return bool(await oauth_store.get_client(credential.removeprefix("oauth:")))
        if item.get("auth_mode") == "bearer" and credential.startswith("bearer:"):
            # No raw bearer token is persisted with a subscription.
            for bearer in credentials.bearer_items():
                token = str(bearer.get("token") or "")
                if not token or not credentials._active(bearer):
                    continue
                identifier = str(
                    bearer.get("id") or hashlib.sha256(token.encode()).hexdigest()[:20]
                )
                if hmac.compare_digest(credential, "bearer:" + identifier):
                    return True
            # Admission also accepts the configured legacy bearer-token list.
            # Recheck current configuration, so removing a token revokes delivery.
            for raw_token in settings.bearer_tokens.split(","):
                token = raw_token.strip()
                if token:
                    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:20]
                    if hmac.compare_digest(credential, "bearer:" + fingerprint):
                        return True
        return False

    message_subscriber_resolver = (
        MeshMessageSubscriberResolver(access_mesh) if access_mesh is not None else None
    )

    async def checked_subscription_access(item):
        if not await mcp_events_access_checker(item):
            return False
        if item.get("name") == "message.received":
            return bool(
                message_subscriber_resolver is not None
                and await message_subscriber_resolver.still_bound(item)
            )
        return True

    mcp_event_store = MCPEventStore(
        settings.database_path,
        managed_authority_node_id,
        auth_database_path=settings.auth_database_path,
    )
    access_mcp = (
        build_access_mcp(
            application,
            event_store=mcp_event_store,
            public_base_url=settings.public_base_url,
        )
        if access_mesh
        else None
    )
    lifecycle_event_mesh = None
    if access_mesh_replication is not None:
        lifecycle_event_mesh = LifecycleEventMesh(
            mcp_event_store,
            access_mesh_replication.request,
            authorization_checker=checked_subscription_access,
        )
        access_mesh_replication.lifecycle_events = lifecycle_event_mesh
    # The native session path owns the one-shot idle clock.
    if access_mesh is not None:
        access_mesh.attach_notifier = mcp_event_store.note_local_attach
        access_mesh.activity_notifier = mcp_event_store.note_managed_activity

    # Access is the sole Events subscription surface. Other role tools and
    # protocol versions continue functioning without advertising Events.
    executor_events = MCPEventsAdapter(
        executor_mcp.streamable_http_app(),
        mcp_event_store,
        detailed_logging=lambda: runtime.current.detailed_logging,
        access_checker=checked_subscription_access,
        endpoint_role="executor",
        events_enabled=False,
    )
    coordinator_events = MCPEventsAdapter(
        coordinator_mcp.streamable_http_app(),
        mcp_event_store,
        detailed_logging=lambda: runtime.current.detailed_logging,
        access_checker=checked_subscription_access,
        endpoint_role="coordinator",
        events_enabled=False,
    )
    access_events = (
        MCPEventsAdapter(
            access_mcp.streamable_http_app(),
            mcp_event_store,
            detailed_logging=lambda: runtime.current.detailed_logging,
            access_checker=checked_subscription_access,
            message_resolver=message_subscriber_resolver,
            endpoint_role="access",
            events_enabled=True,
        )
        if access_mcp
        else None
    )

    async def migrate_runtime_state():
        return await asyncio.to_thread(
            migrate_unified_runtime,
            mesh_store,
            defaults=access_mesh.configured_defaults,
            existing_installation=existing_runtime,
            preserve_local_defaults=migrated_defaults is not None,
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Finish an authenticated, committed Join/Detach interrupted before
        # its config/metadata write, before identity checks initialise stores.
        await sync_namespace()
        events.start()
        await runtime.start()
        await metrics.start()
        events.emit("application_started", outcome="success")
        await repo.initialize(
            preserve_active_commands=bool(getattr(execution, "reconnectable", False))
        )
        await migrate_runtime_state()
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
        await oauth_store.initialize()
        await auth_foundation.initialize()
        await pairing_store.initialize()
        if fresh_identity_install:
            # An explicit pristine state (no main DB, auth DB, or Access key)
            # is the only supported bootstrap path. Seal it before serving.
            initialize_fresh_identity(settings.database_path, settings.auth_database_path)
        await terminal.start()
        if fleet_projection_service:
            await fleet_projection_service.start()
        if managed_fleet_control:
            await managed_fleet_control.start()
        if access_mesh:
            await access_mesh.start()
        if access_mesh_messages:
            await access_mesh_messages.start()
        try:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(executor_mcp.session_manager.run())
                await stack.enter_async_context(coordinator_mcp.session_manager.run())
                if access_mcp:
                    await stack.enter_async_context(access_mcp.session_manager.run())
                await access_events.start()
                if access_mesh_replication:
                    # Start only after one source/outbox initialization pass.
                    await access_mesh_replication.start()
                try:
                    yield
                finally:
                    await access_events.stop()
        finally:
            if access_mesh_messages:
                await access_mesh_messages.stop()
            if access_mesh_replication:
                await access_mesh_replication.stop()
            if access_mesh:
                await access_mesh.stop()
            if managed_fleet_control:
                await managed_fleet_control.stop()
            if fleet_projection_service:
                await fleet_projection_service.stop()
            await terminal.stop()
            events.emit("application_stopped", outcome="success")
            await metrics.stop()
            await runtime.stop()
            events.stop()

    app = FastAPI(title="terminal-mcp", version=__version__, lifespan=lifespan)
    app.state.access_mesh = access_mesh
    app.state.access_mesh_messages = access_mesh_messages
    app.state.access_mesh_replication = access_mesh_replication
    app.state.access_mcp = access_mcp
    app.state.access_events = access_events
    app.state.message_subscriber_resolver = message_subscriber_resolver
    app.state.mcp_events = access_events
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
    app.state.migrate_runtime_state = migrate_runtime_state
    app.state.fleet_replication = fleet_replication
    app.state.fleet_node_meta = fleet_node_meta
    app.state.fleet_source = fleet_source
    app.state.fleet_control = fleet_control
    app.state.managed_fleet_control = managed_fleet_control
    app.state.fleet_projection = fleet_projection
    app.state.fleet_projection_service = fleet_projection_service
    app.state.persistent_backend = service.persistent
    app.state.persistent_lifecycle = persistent_lifecycle
    app.state.persistent_policy_controller = persistent_policy_controller
    app.include_router(build_public_router())
    if access_mesh:
        app.include_router(build_access_mesh_operator_router(application))
    if access_mesh and fleet_replication:
        # Managed Fleet Control changes fleet_replication.config in place at
        # startup. Access Mesh outbound uses the bootstrap config; inbound
        # authentication MUST use that same pinned trust/token material.
        mesh_peer_auth = PinnedAccessMeshPeerAuth(fleet_config, access_mesh_replication)
        app.include_router(build_access_mesh_router(access_mesh, mesh_peer_auth))
        if lifecycle_event_mesh is not None:
            app.include_router(
                build_lifecycle_event_router(
                    lifecycle_event_mesh,
                    mesh_peer_auth,
                    access_mesh,
                )
            )
        if lifecycle_event_mesh is not None:
            app.include_router(
                build_message_event_router(
                    lifecycle_event_mesh.message_events,
                    mesh_peer_auth,
                    access_mesh,
                )
            )
        app.include_router(build_access_mesh_message_router(access_mesh_messages, mesh_peer_auth))
    if fleet_replication:
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
        if managed_fleet_control:
            app.include_router(
                build_fleet_control_router(
                    managed_fleet_control, fleet_replication, application=application.fleet_control
                )
            )
            app.include_router(
                build_mesh_vpn_control_router(
                    managed_fleet_control, application.fleet_control, settings
                )
            )
    app.include_router(build_pairing_router(settings, auth, pairing_store))
    app.include_router(build_runtime_router(settings))
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
    if fleet_projection:
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
    app.include_router(build_console_router(service, settings))
    app.include_router(build_admin_router(settings, credentials, oauth_store, terminal, service))
    app.router.routes.append(Mount("/terminal-mcp/executor/v1/mcp", app=executor_events))
    app.router.routes.append(Mount("/terminal-mcp/coordinator/v1/mcp", app=coordinator_events))
    if access_mcp:
        app.router.routes.append(Mount("/terminal-mcp/access/v1/mcp", app=access_events))

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"ok": True, "version": __version__, "release": __release__}

    @app.get("/health/ready", include_in_schema=False)
    async def ready():
        try:
            state = await asyncio.to_thread(access_mesh.registry_readiness)
        except Exception:
            state = {"ready": False, "reason": "mesh_registry_unavailable"}
        return JSONResponse(
            {"ok": state["ready"], "version": __version__, "release": __release__, **state},
            status_code=200 if state["ready"] else 503,
        )

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
    app.add_middleware(TraceMiddleware, events=events)
    app.add_middleware(BrowserSecurityMiddleware, settings=settings)
    # Opt-in probe records Events ingress even if authentication rejects it.
    app.add_middleware(MCPEventIngressProbe, enabled=lambda: runtime.current.detailed_logging)
    return app
