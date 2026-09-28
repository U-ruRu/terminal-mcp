from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

CONNECT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Terminal MCP Console pairing</title></head>
<body><h1>Terminal MCP Console pairing</h1>
<p>This one-time link must be opened by a Terminal MCP Console client.</p>
<p>The pairing secret stays in the URL fragment and is never sent by this page.</p>
</body></html>"""


class PairingExchangeRequest(BaseModel):
    secret: str = Field(min_length=32, max_length=512)
    public_key: str = Field(min_length=32, max_length=8192)
    device_label: str = Field(min_length=1, max_length=100)


def build_pairing_router(settings, auth, pairing_store):
    router = APIRouter()

    @router.get("/connect", response_class=HTMLResponse, include_in_schema=False)
    async def connect():
        return HTMLResponse(CONNECT_PAGE)

    @router.post("/pairing/exchange", include_in_schema=False)
    async def exchange(request: Request):
        try:
            body = await request.json()
            model = PairingExchangeRequest.model_validate(body)
            label = model.device_label.strip()
            public_key = model.public_key.strip()
            if not label or not public_key:
                raise ValueError("blank device metadata")
        except (ValueError, TypeError, ValidationError):
            await pairing_store.record_audit(
                "pairing_exchange", "rejected", "malformed"
            )
            return JSONResponse({"error": "invalid_request"}, 400)

        scope = " ".join(dict.fromkeys(settings.oauth_required_scopes.split()))
        result, _reason = await pairing_store.exchange(
            model.secret,
            public_key,
            label,
            scope,
            settings.oauth_refresh_ttl_sec,
        )
        if result is None:
            return JSONResponse({"error": "invalid_pairing"}, 400)

        access = auth.issue_access(result.client_id, result.scope)
        return {
            "device_id": result.device_id,
            "client_id": result.client_id,
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": settings.oauth_access_ttl_sec,
            "refresh_token": result.refresh_token,
            "scope": result.scope,
        }

    return router
