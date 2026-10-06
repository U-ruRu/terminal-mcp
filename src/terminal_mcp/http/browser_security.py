from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

_ALLOWED_METHODS = {"GET", "POST", "OPTIONS"}
_ALLOWED_REQUEST_HEADERS = {"authorization", "content-type"}


def canonical_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("browser origins must use http or https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("browser origins must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("browser origins must not contain a path")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{suffix}"


def browser_transport_path(path: str) -> bool:
    return (
        path == "/connect"
        or path == "/pairing/exchange"
        or path == "/oauth/token"
        or path.startswith("/actions/")
        or path.startswith("/console/")
        or path.startswith("/access/")
    )


def _apply_security_headers(response: Response, path: str) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if path == "/connect":
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"


class BrowserSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings):
        super().__init__(app)
        self.allowed_origins = frozenset(settings.browser_allowed_origins())

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not browser_transport_path(path):
            return await call_next(request)

        origin = request.headers.get("origin")
        normalized_origin = ""
        if origin:
            try:
                normalized_origin = canonical_origin(origin)
            except ValueError:
                pass
            if normalized_origin not in self.allowed_origins:
                response = JSONResponse({"error": "origin_not_allowed"}, status_code=403)
                _apply_security_headers(response, path)
                return response

        requested_method = request.headers.get("access-control-request-method")
        if request.method == "OPTIONS" and requested_method:
            requested_headers = {
                item.strip().lower()
                for item in request.headers.get("access-control-request-headers", "").split(",")
                if item.strip()
            }
            if (
                not normalized_origin
                or requested_method.upper() not in _ALLOWED_METHODS
                or not requested_headers.issubset(_ALLOWED_REQUEST_HEADERS)
            ):
                response = JSONResponse({"error": "cors_preflight_rejected"}, status_code=403)
                _apply_security_headers(response, path)
                return response
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Origin"] = normalized_origin
            response.headers["Access-Control-Allow-Methods"] = ", ".join(sorted(_ALLOWED_METHODS))
            if requested_headers:
                response.headers["Access-Control-Allow-Headers"] = ", ".join(
                    sorted(requested_headers)
                )
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
            _apply_security_headers(response, path)
            return response

        response = await call_next(request)
        if normalized_origin:
            response.headers["Access-Control-Allow-Origin"] = normalized_origin
            vary = response.headers.get("Vary")
            response.headers["Vary"] = (
                f"{vary}, Origin" if vary and "Origin" not in vary else (vary or "Origin")
            )
        _apply_security_headers(response, path)
        return response
