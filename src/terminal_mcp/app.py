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
from terminal_mcp.core.service import TerminalService
from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.admin import build_admin_router
from terminal_mcp.http.public import build_public_router
from terminal_mcp.http.rate_limit import RateLimitMiddleware
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repo = SqliteRepository(settings.database_path)
    oauth_store = OAuthStore(settings.database_path)
    credentials = CredentialManager(settings)
    terminal = LinuxTerminalAdapter(
        repo, settings.shell, settings.cwd, settings.cancel_grace_sec, settings.terminal_user
    )
    service = TerminalService(
        repo, terminal, settings.max_read_lines, settings.auth_mode, settings.health_command
    )
    auth = AuthService(settings, oauth_store, credentials)
    mcp = build_mcp(service, settings.public_base_url, settings.mode_for("mcp"))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await repo.initialize()
        await oauth_store.initialize()
        await terminal.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await terminal.stop()

    app = FastAPI(title="terminal-mcp", version="0.5.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.oauth_store = oauth_store
    app.state.credentials = credentials
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
    return app


app = create_app()
