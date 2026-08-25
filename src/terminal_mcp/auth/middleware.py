from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

PUBLIC_PREFIXES = (
    "/.well-known/",
    "/mcp/.well-known/",
    "/oauth/",
    "/docs",
    "/openapi.json",
    "/redoc",
)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings, auth_service):
        super().__init__(app)
        self.s = settings
        self.auth = auth_service

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/health/live" or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        interface = (
            "mcp" if path.startswith("/mcp") else "actions" if path.startswith("/actions") else None
        )
        if not interface:
            return await call_next(request)
        mode = self.s.mode_for(interface)
        if mode == "none":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            return self._deny("missing_token")
        try:
            if mode == "bearer":
                if not self.auth.bearer_valid(token):
                    raise PermissionError("invalid_token")
            elif mode == "oauth":
                request.state.oauth_claims = await self.auth.verify_access(
                    token, self._scopes(path, request.method)
                )
            else:
                raise PermissionError("unsupported_auth_mode")
        except Exception as exc:
            return self._deny(str(exc))
        return await call_next(request)

    @staticmethod
    def _scopes(path, method):
        return (
            ["terminal:execute"]
            if path.endswith("/run") or path.endswith("/cancel") or path.startswith("/mcp")
            else ["terminal:read"]
        )

    def _deny(self, detail):
        metadata = f"{self.s.public_base_url}/.well-known/oauth-protected-resource/mcp"
        header = f'Bearer resource_metadata="{metadata}"'
        return JSONResponse(
            {"error": "unauthorized", "detail": detail},
            401,
            headers={"WWW-Authenticate": header},
        )
