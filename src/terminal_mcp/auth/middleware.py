from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from terminal_mcp.auth.admission import verified_admission_context
from terminal_mcp.core.persistent_admission import bind_admission_context, reset_admission_context

PUBLIC_PREFIXES = (
    "/.well-known/",
    "/mcp/.well-known/",
    "/oauth/",
    "/docs",
    "/openapi.json",
    "/redoc",
)

# In mixed deployments actions remain protected by the legacy static bearer token,
# while the paired Console authenticates with its short-lived OAuth device token.
# Keep that OAuth fallback narrowly scoped to the Console control plane: snapshot
# reads plus Persistent slot/claim mutations. Agent execution/session/task routes
# deliberately remain unavailable through this fallback.
PAIRED_CONSOLE_PERSISTENT_MUTATIONS = frozenset(
    {
        "/actions/persistent/policy",
        "/actions/persistent/slots/create",
        "/actions/persistent/slots/migrate-access",
        "/actions/persistent/slots/rotate-access-code",
        "/actions/persistent/slots/rotate-selector",
        "/actions/persistent/slots/play",
        "/actions/persistent/slots/suspend",
        "/actions/persistent/slots/delete",
        "/actions/persistent/claims/release",
        "/actions/persistent/claims/reassign",
    }
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
        context_token = bind_admission_context(None)
        if mode == "none":
            try:
                return await call_next(request)
            finally:
                reset_admission_context(context_token)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            reset_admission_context(context_token)
            return self._deny("missing_token")
        claims = None
        effective_mode = mode
        try:
            if mode == "bearer":
                if not self.auth.bearer_valid(token):
                    paired_scopes = self._paired_console_scopes(path, request.method)
                    if paired_scopes is None:
                        raise PermissionError("invalid_token")
                    try:
                        claims = await self.auth.verify_access(token, paired_scopes)
                        client_id = str(claims.get("sub", ""))
                        device = await self.pairing_store.active_device_for_client(client_id)
                        if not client_id or device is None:
                            raise PermissionError("invalid_token")
                        request.state.oauth_claims = claims
                        effective_mode = "oauth"
                    except Exception as exc:
                        raise PermissionError("invalid_token") from exc
            elif mode == "oauth":
                claims = await self.auth.verify_access(token, self._scopes(path, request.method))
                request.state.oauth_claims = claims
            else:
                raise PermissionError("unsupported_auth_mode")
            admission = verified_admission_context(
                auth_mode=effective_mode,
                token=token,
                claims=claims,
                credentials=self.auth.credentials,
                transport=interface,
            )
            reset_admission_context(context_token)
            context_token = bind_admission_context(admission)
        except Exception as exc:
            reset_admission_context(context_token)
            return self._deny(str(exc))
        try:
            return await call_next(request)
        finally:
            reset_admission_context(context_token)

    @staticmethod
    def _paired_console_scopes(path, method):
        if method.upper() == "GET" and path == "/actions/console/snapshot":
            return ["terminal:read"]
        if method.upper() == "POST" and path in PAIRED_CONSOLE_PERSISTENT_MUTATIONS:
            return ["terminal:read", "terminal:execute"]
        return None

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
