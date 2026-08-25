# ruff: noqa: E501
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

PRIVACY = """<!doctype html><html><meta charset="utf-8"><title>Privacy Policy</title><body><h1>Privacy Policy</h1><p>terminal-mcp executes commands on the server selected by its owner. Command text, terminal output, authorization records, OAuth clients and technical audit data may be stored locally on that server. The service owner controls retention and deletion. Data is not sold. Connected clients receive data only when they call an authorized tool or endpoint. Credentials are used only to authenticate access to this service. Contact the service owner for access or deletion requests.</p><p>Effective date: 2026-07-15.</p></body></html>"""


def build_public_router():
    r = APIRouter()

    @r.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    async def privacy():
        return HTMLResponse(PRIVACY)

    return r
