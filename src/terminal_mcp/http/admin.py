# ruff: noqa: E501
import hashlib
import hmac
import secrets
import time
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse


def _sig(secret, value):
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _session(s):
    value = f"{s.admin_username}:{int(time.time()) // 86400}"
    return value + "." + _sig(s.admin_session_secret, value)


def _valid(s, cookie):
    if not cookie or "." not in cookie:
        return False
    value, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sig(s.admin_session_secret, value)):
        return False
    try:
        username, day = value.rsplit(":", 1)
        return (
            hmac.compare_digest(username, s.admin_username)
            and int(day) == int(time.time()) // 86400
        )
    except (ValueError, TypeError):
        return False


def _csrf(s, session):
    return _sig(s.admin_session_secret, "csrf:" + session)


def _page(body):
    return HTMLResponse(
        f'<!doctype html><html><meta charset="utf-8"><title>terminal-mcp</title><body><h1>terminal-mcp</h1>{body}</body></html>'
    )


def build_admin_router(settings, credentials, oauth_store, terminal, service):
    r = APIRouter()

    @r.get("/admin/login")
    async def login_page():
        token = secrets.token_urlsafe(32)
        response = _page(
            f'<form method="post"><input type="hidden" name="csrf_token" value="{token}"><input name="username"><input type="password" name="password"><button>Login</button></form>'
        )
        response.set_cookie("tmcp_login_csrf", token, httponly=True, secure=True, samesite="strict")
        return response

    @r.post("/admin/login")
    async def login(
        request: Request, username: str = Form(), password: str = Form(), csrf_token: str = Form()
    ):
        csrf_cookie = request.cookies.get("tmcp_login_csrf", "")
        csrf_ok = bool(csrf_cookie) and hmac.compare_digest(csrf_cookie, csrf_token)
        ok = (
            csrf_ok
            and hmac.compare_digest(username, settings.admin_username)
            and hmac.compare_digest(password, settings.admin_password)
        )
        if not ok:
            return _page("<p>Invalid credentials</p>")
        response = RedirectResponse("/admin", 303)
        response.set_cookie(
            "tmcp_admin", _session(settings), httponly=True, secure=True, samesite="strict"
        )
        return response

    def guard(request):
        return _valid(settings, request.cookies.get("tmcp_admin"))

    async def csrf_guard(request):
        session = request.cookies.get("tmcp_admin", "")
        if not _valid(settings, session):
            return False
        form = await request.form()
        return hmac.compare_digest(str(form.get("csrf_token", "")), _csrf(settings, session))

    @r.get("/admin")
    async def dashboard(request: Request):
        if not guard(request):
            return RedirectResponse("/admin/login", 303)
        session = request.cookies.get("tmcp_admin", "")
        hidden = (
            f'<input type="hidden" name="csrf_token" value="{escape(_csrf(settings, session))}">'
        )
        bearers = "".join(
            f'<li>{escape(x.get("name", ""))}: <code>{escape(x.get("token", ""))}</code> exp={escape(x.get("expires_at", ""))} <form method="post" action="/admin/bearer/{x.get("id")}/delete" style="display:inline">{hidden}<button>Delete</button></form></li>'
            for x in credentials.bearer_items()
        )
        users = "".join(
            f'<li>{escape(x.get("username", ""))}: <code>{escape(x.get("password", ""))}</code> exp={escape(x.get("expires_at", ""))} <form method="post" action="/admin/oauth-user/{x.get("id")}/delete" style="display:inline">{hidden}<button>Delete</button></form></li>'
            for x in credentials.oauth_users()
        )
        clients = "".join(
            f'<li>{escape(x[3])} ({escape(x[0])}) <form method="post" action="/admin/oauth-client/{x[0]}/delete" style="display:inline">{hidden}<button>Revoke</button></form></li>'
            for x in await oauth_store.list_clients()
        )
        body = f'<h2>Admin UI credentials</h2><p>Login: <code>{escape(settings.admin_username)}</code></p><p>Password: <code>{escape(settings.admin_password)}</code></p><h2>Terminal</h2><form method="post" action="/admin/runtime">{hidden}<input name="cwd" value="{escape(str(settings.cwd))}"><input name="user" value="{escape(settings.terminal_user)}"><br><label>Health command<br><textarea name="health_command" rows="4" cols="80">{escape(settings.health_command)}</textarea></label><br><button>Save</button></form><h2>Bearer</h2><ul>{bearers}</ul><form method="post" action="/admin/bearer">{hidden}<input name="name"><input name="expires_at"><button>Add</button></form><h2>OAuth logins</h2><ul>{users}</ul><form method="post" action="/admin/oauth-user">{hidden}<input name="username"><input name="password"><input name="expires_at"><button>Add</button></form><h2>OAuth clients</h2><ul>{clients}</ul>'
        return _page(body)

    @r.post("/admin/runtime")
    async def runtime(
        request: Request, cwd: str = Form(), user: str = Form(), health_command: str = Form("")
    ):
        if await csrf_guard(request):
            credentials.update_runtime(cwd, user, health_command)
            terminal.cwd = settings.cwd
            terminal.user = settings.terminal_user
            service.health_command = settings.health_command
        return RedirectResponse("/admin", 303)

    @r.post("/admin/bearer")
    async def add_bearer(request: Request, name: str = Form(), expires_at: str = Form("")):
        if await csrf_guard(request):
            credentials.add_bearer(name, expires_at)
        return RedirectResponse("/admin", 303)

    @r.post("/admin/bearer/{item_id}/delete")
    async def delete_bearer(item_id: str, request: Request):
        if await csrf_guard(request):
            credentials.delete("bearer", item_id)
        return RedirectResponse("/admin", 303)

    @r.post("/admin/oauth-user")
    async def add_user(
        request: Request, username: str = Form(), password: str = Form(), expires_at: str = Form("")
    ):
        if await csrf_guard(request):
            credentials.add_oauth_user(username, password, expires_at)
        return RedirectResponse("/admin", 303)

    @r.post("/admin/oauth-user/{item_id}/delete")
    async def delete_user(item_id: str, request: Request):
        if await csrf_guard(request):
            credentials.delete("oauth", item_id)
        return RedirectResponse("/admin", 303)

    @r.post("/admin/oauth-client/{client_id}/delete")
    async def delete_client(client_id: str, request: Request):
        if await csrf_guard(request):
            await oauth_store.delete_client(client_id)
        return RedirectResponse("/admin", 303)

    return r
