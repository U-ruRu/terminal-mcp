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
    def __init__(self, app, settings, auth_service, pairing_store):
        super().__init__(app)
        self.s = settings
        self.auth = auth_service
        self.pairing_store = pairing_store

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
                    if path != "/actions/console/snapshot":
                        raise PermissionError("invalid_token")
                    try:
                        claims = await self.auth.verify_access(
                            token, self._scopes(path, request.method)
                        )
                        client_id = str(claims.get("sub", ""))
                        device = await self.pairing_store.active_device_for_client(client_id)
                        if not client_id or device is None:
                            raise PermissionError("invalid_token")
                        request.state.oauth_claims = claims
                    except Exception as exc:
                        raise PermissionError("invalid_token") from exc
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
        # All agent-facing tools intentionally share one non-escalating OAuth scope.
        # Application session/coordination guardrails remain authoritative.
        return ["terminal:read"]

    def _deny(self, detail):
        metadata = f"{self.s.public_base_url}/.well-known/oauth-protected-resource/mcp"
        header = f'Bearer resource_metadata="{metadata}"'
        return JSONResponse(
            {"error": "unauthorized", "detail": detail},
            401,
            headers={"WWW-Authenticate": header},
        )
