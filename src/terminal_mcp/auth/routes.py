import base64
import hmac
import logging
import re
from html import escape
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from terminal_mcp.auth.credentials import OAuthCredentialFileError

logger = logging.getLogger(__name__)

FORM = """<!doctype html><html><body><h1>terminal-mcp</h1><form method="post"><input type="hidden" name="client_id" value="{client_id}"><input type="hidden" name="redirect_uri" value="{redirect_uri}"><input type="hidden" name="scope" value="{scope}"><input type="hidden" name="state" value="{state}"><input type="hidden" name="code_challenge" value="{code_challenge}"><input type="hidden" name="code_challenge_method" value="S256"><label>Username <input name="username"></label><label>Password <input type="password" name="password"></label><button>Authorize</button></form></body></html>"""  # noqa: E501


_CHATGPT_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")
_CHATGPT_CALLBACK_PATH = re.compile(r"/connector/oauth/[A-Za-z0-9_-]{8,128}")
_S256_CODE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43,128}")


def _chatgpt_first_login_allowed(
    client_id: str, redirect_uri: str, code_challenge: str, code_challenge_method: str
) -> bool:
    """Allow a new ChatGPT public client only with its exact, HTTPS callback and PKCE."""
    if (
        not _CHATGPT_CLIENT_ID.fullmatch(client_id)
        or not _S256_CODE_CHALLENGE.fullmatch(code_challenge)
        or code_challenge_method != "S256"
        or any(ord(char) < 32 for char in redirect_uri)
    ):
        return False
    try:
        callback = urlsplit(redirect_uri)
    except ValueError:
        return False
    return (
        callback.scheme == "https"
        and callback.netloc == "chatgpt.com"
        and bool(_CHATGPT_CALLBACK_PATH.fullmatch(callback.path))
        and not callback.query
        and not callback.fragment
    )


def build_oauth_router(settings, auth, store):
    r = APIRouter()
    issuer = settings.oauth_issuer or settings.public_base_url

    @r.get("/.well-known/oauth-authorization-server")
    async def authorization_metadata():
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{settings.public_base_url}/oauth/authorize",
            "token_endpoint": f"{settings.public_base_url}/oauth/token",
            "registration_endpoint": f"{settings.public_base_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_basic",
                "client_secret_post",
            ],
            "scopes_supported": settings.oauth_required_scopes.split(),
        }

    def protected_resource_metadata(resource: str | None = None):
        return {
            "resource": resource
            or settings.oauth_audience
            or f"{settings.public_base_url}/terminal-mcp/access/v1/mcp",
            "authorization_servers": [issuer],
            "scopes_supported": settings.oauth_required_scopes.split(),
            "bearer_methods_supported": ["header"],
        }

    @r.get("/.well-known/oauth-protected-resource")
    async def protected_metadata():
        return protected_resource_metadata()

    @r.get("/.well-known/oauth-protected-resource/terminal-mcp/access/v1/mcp")
    @r.get("/terminal-mcp/access/v1/mcp/.well-known/oauth-protected-resource")
    async def access_protected_metadata():
        return protected_resource_metadata(f"{settings.public_base_url}/terminal-mcp/access/v1/mcp")

    @r.get("/.well-known/oauth-protected-resource/terminal-mcp/executor/v1/mcp")
    @r.get("/terminal-mcp/executor/v1/mcp/.well-known/oauth-protected-resource")
    async def executor_protected_metadata():
        return protected_resource_metadata(
            f"{settings.public_base_url}/terminal-mcp/executor/v1/mcp"
        )

    @r.get("/.well-known/oauth-protected-resource/terminal-mcp/coordinator/v1/mcp")
    @r.get("/terminal-mcp/coordinator/v1/mcp/.well-known/oauth-protected-resource")
    async def coordinator_protected_metadata():
        return protected_resource_metadata(
            f"{settings.public_base_url}/terminal-mcp/coordinator/v1/mcp"
        )

    @r.post("/oauth/register")
    async def register(request: Request):
        body = await request.json()
        uris = body.get("redirect_uris") or []
        method = body.get("token_endpoint_auth_method", "none")
        if not uris or method not in {"none", "client_secret_basic", "client_secret_post"}:
            return JSONResponse({"error": "invalid_client_metadata"}, 400)
        cid, secret = await store.register_client(
            uris, body.get("client_name", "OAuth client"), method
        )
        out = {
            "client_id": cid,
            "client_id_issued_at": int(__import__("time").time()),
            "client_secret_expires_at": 0,
            "redirect_uris": uris,
            "client_name": body.get("client_name", "OAuth client"),
            "token_endpoint_auth_method": method,
            "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
            "response_types": body.get("response_types", ["code"]),
        }
        if secret:
            out["client_secret"] = secret
        return JSONResponse(out, 201)

    @r.get("/oauth/authorize", response_class=HTMLResponse)
    async def authorize_form(
        client_id: str,
        redirect_uri: str,
        response_type: str,
        scope: str = "",
        state: str = "",
        code_challenge: str = "",
        code_challenge_method: str = "",
    ):
        client = await store.get_client(client_id)
        redirect_allowed = (
            redirect_uri in client[2].split("\n")
            if client
            else _chatgpt_first_login_allowed(
                client_id, redirect_uri, code_challenge, code_challenge_method
            )
        )
        if (
            response_type != "code"
            or not redirect_allowed
            or code_challenge_method != "S256"
            or not code_challenge
        ):
            return HTMLResponse("invalid authorization request", 400)
        return HTMLResponse(
            FORM.format(
                client_id=escape(client_id),
                redirect_uri=escape(redirect_uri),
                scope=escape(scope),
                state=escape(state),
                code_challenge=escape(code_challenge),
            )
        )

    @r.post("/oauth/authorize")
    async def authorize_submit(
        client_id: str = Form(),
        redirect_uri: str = Form(),
        scope: str = Form(""),
        state: str = Form(""),
        code_challenge: str = Form(),
        code_challenge_method: str = Form(),
        username: str = Form(),
        password: str = Form(),
    ):
        client = await store.get_client(client_id)
        first_login = not client and _chatgpt_first_login_allowed(
            client_id, redirect_uri, code_challenge, code_challenge_method
        )
        requested = set(scope.split())
        allowed = set(settings.oauth_required_scopes.split())
        valid = (
            (bool(client and redirect_uri in client[2].split("\n")) or first_login)
            and code_challenge_method == "S256"
            and bool(code_challenge)
            and requested.issubset(allowed)
        )
        if valid:
            try:
                valid = auth.oauth_user_valid(username, password)
            except OAuthCredentialFileError as exc:
                logger.error("OAuth authorization unavailable: %s", exc)
                return HTMLResponse("authorization unavailable", 503)
        if not valid:
            return HTMLResponse("authorization denied", 403)
        if first_login:
            # The browser must prove the owner's credentials *before* we persist
            # an externally supplied client ID. Never broaden an existing binding.
            registered = await store.register_authenticated_chatgpt_client(client_id, redirect_uri)
            if (
                not registered
                or registered[4] != "none"
                or redirect_uri not in registered[2].split("\n")
            ):
                return HTMLResponse("authorization denied", 403)
        code = await store.create_code(
            client_id,
            redirect_uri,
            " ".join(sorted(requested)),
            code_challenge,
            settings.oauth_code_ttl_sec,
        )
        return RedirectResponse(auth.redirect(redirect_uri, {"code": code, "state": state}), 303)

    async def authenticate_client(request: Request, client_id, client_secret):
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                client_id, client_secret = (
                    base64.b64decode(header.split(" ", 1)[1]).decode().split(":", 1)
                )
            except Exception:
                return None
        client = await store.get_client(client_id or "")
        if not client:
            return None
        if client[4] == "none":
            return client
        return (
            client
            if client_secret and hmac.compare_digest(store.digest(client_secret), client[1])
            else None
        )

    @r.post("/oauth/token")
    async def token(
        request: Request,
        grant_type: str = Form(),
        code: str | None = Form(None),
        redirect_uri: str | None = Form(None),
        code_verifier: str | None = Form(None),
        refresh_token: str | None = Form(None),
        client_id: str | None = Form(None),
        client_secret: str | None = Form(None),
    ):
        client = await authenticate_client(request, client_id, client_secret)
        if not client:
            return JSONResponse({"error": "invalid_client"}, 401)
        if grant_type == "authorization_code":
            record = await store.get_code(code or "")
            if (
                not record
                or record[0] != client[0]
                or record[1] != redirect_uri
                or not code_verifier
                or not auth.pkce_ok(code_verifier, record[3])
            ):
                return JSONResponse({"error": "invalid_grant"}, 400)
            if not await store.consume_code(code or ""):
                return JSONResponse({"error": "invalid_grant"}, 400)
            scope = record[2]
        elif grant_type == "refresh_token":
            rotated = await store.rotate_refresh(refresh_token or "")
            if not rotated or rotated[0] != client[0]:
                return JSONResponse({"error": "invalid_grant"}, 400)
            scope = rotated[1]
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, 400)
        access = auth.issue_access(client[0], scope)
        refresh = await store.create_refresh(client[0], scope, settings.oauth_refresh_ttl_sec)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": settings.oauth_access_ttl_sec,
            "refresh_token": refresh,
            "scope": scope,
        }

    return r
