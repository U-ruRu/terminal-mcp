import base64
import hmac
import json
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}
FORM = """<!doctype html><html><body><h1>terminal-mcp</h1><form method="post"><input type="hidden" name="client_id" value="{client_id}"><input type="hidden" name="redirect_uri" value="{redirect_uri}"><input type="hidden" name="scope" value="{scope}"><input type="hidden" name="state" value="{state}"><input type="hidden" name="code_challenge" value="{code_challenge}"><input type="hidden" name="code_challenge_method" value="S256"><label>Username <input name="username"></label><label>Password <input type="password" name="password"></label><button>Authorize</button></form><script>addEventListener("pageshow",event=>{{if(event.persisted)location.reload()}});</script></body></html>"""  # noqa: E501
COMPLETED = """<!doctype html><html><body><h1>terminal-mcp</h1><p>This authorization request has already been completed. This window can be closed.</p><script>window.close();</script></body></html>"""  # noqa: E501


def _authorization_request_hash(store, client_id, redirect_uri, scope, state, code_challenge):
    payload = json.dumps(
        ["v1", client_id, redirect_uri, scope, state, code_challenge],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return store.digest(payload)


def _no_store_html(content, status_code=200):
    return HTMLResponse(content, status_code=status_code, headers=NO_STORE_HEADERS)


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

    def protected_resource_metadata():
        return {
            "resource": settings.oauth_audience or f"{settings.public_base_url}/mcp",
            "authorization_servers": [issuer],
            "scopes_supported": settings.oauth_required_scopes.split(),
            "bearer_methods_supported": ["header"],
        }

    @r.get("/.well-known/oauth-protected-resource")
    @r.get("/.well-known/oauth-protected-resource/mcp")
    @r.get("/mcp/.well-known/oauth-protected-resource")
    async def protected_metadata():
        return protected_resource_metadata()

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
        if (
            response_type != "code"
            or not client
            or redirect_uri not in client[2].split("\n")
            or code_challenge_method != "S256"
            or not code_challenge
        ):
            return _no_store_html("invalid authorization request", 400)
        request_hash = _authorization_request_hash(
            store, client_id, redirect_uri, scope, state, code_challenge
        )
        if await store.authorization_request_used(request_hash):
            return _no_store_html(COMPLETED, 410)
        return _no_store_html(
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
        requested = set(scope.split())
        allowed = set(settings.oauth_required_scopes.split())
        valid = (
            client
            and redirect_uri in client[2].split("\n")
            and code_challenge_method == "S256"
            and code_challenge
            and requested.issubset(allowed)
        )
        valid = valid and auth.oauth_user_valid(username, password)
        if not valid:
            return _no_store_html("authorization denied", 403)
        request_hash = _authorization_request_hash(
            store, client_id, redirect_uri, scope, state, code_challenge
        )
        code = await store.create_code_once(
            request_hash,
            client_id,
            redirect_uri,
            " ".join(sorted(requested)),
            code_challenge,
            settings.oauth_code_ttl_sec,
        )
        if code is None:
            return _no_store_html(COMPLETED, 410)
        return RedirectResponse(
            auth.redirect(redirect_uri, {"code": code, "state": state}),
            303,
            headers=NO_STORE_HEADERS,
        )

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
