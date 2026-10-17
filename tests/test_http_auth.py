"""Atomic OAuth HTTP operations with independent prepared authorization state."""

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from auth_http_test_helpers import pkce, settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.auth.routes import build_oauth_router
from terminal_mcp.auth.service import AuthService
from terminal_mcp.auth.storage import OAuthStore

CALLBACK = "https://chat.example/callback"
VERIFIER = "v" * 64


@pytest.fixture
def oauth(tmp_path):
    config = settings(tmp_path, auth_mode="oauth")
    store = OAuthStore(tmp_path / "oauth.sqlite3")
    asyncio.run(store.initialize())
    client_id, _ = asyncio.run(store.register_client([CALLBACK], "ChatGPT", "none"))
    credentials = SimpleNamespace(
        oauth_user_valid=lambda username, password: (username, password) == ("admin", "secret")
    )
    auth = AuthService(config, store, credentials)
    app = FastAPI()
    app.include_router(build_oauth_router(config, auth, store))
    with TestClient(app, follow_redirects=False) as client:
        yield client, store, client_id


def test_oauth_register_returns_usable_client(oauth):
    client, store, _ = oauth

    response = client.post(
        "/oauth/register",
        json={
            "client_name": "Connector",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
        },
    )

    assert response.status_code == 201
    registered = asyncio.run(store.get_client(response.json()["client_id"]))
    assert registered[2] == CALLBACK
    assert registered[3] == "Connector"


def test_oauth_authorization_returns_bound_pkce_code(oauth):
    client, store, client_id = oauth

    response = client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "scope": "terminal:read",
            "state": "request-state",
            "code_challenge": pkce(VERIFIER),
            "code_challenge_method": "S256",
            "username": "admin",
            "password": "secret",
        },
    )

    assert response.status_code == 303
    redirect = urlparse(response.headers["location"])
    query = parse_qs(redirect.query)
    assert response.headers["location"].startswith(CALLBACK + "?")
    assert query["state"] == ["request-state"]
    record = asyncio.run(store.get_code(query["code"][0]))
    assert record[:4] == (client_id, CALLBACK, "terminal:read", pkce(VERIFIER))


def test_oauth_token_exchanges_valid_pkce_code(oauth):
    client, store, client_id = oauth
    code = asyncio.run(store.create_code(client_id, CALLBACK, "terminal:read", pkce(VERIFIER), 60))

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": VERIFIER,
        },
    )

    assert response.status_code == 200
    tokens = response.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == "terminal:read"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert asyncio.run(store.get_code(code)) is None


def test_oauth_token_rejects_wrong_pkce_verifier(oauth):
    client, store, client_id = oauth
    code = asyncio.run(store.create_code(client_id, CALLBACK, "terminal:read", pkce(VERIFIER), 60))

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": "wrong-verifier",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_grant"}


def test_oauth_refresh_returns_rotated_credential(oauth):
    client, store, client_id = oauth
    refresh = asyncio.run(store.create_refresh(client_id, "terminal:read", 60))

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
        },
    )

    assert response.status_code == 200
    assert response.json()["refresh_token"] != refresh
    assert response.json()["scope"] == "terminal:read"
    assert asyncio.run(store.rotate_refresh(refresh)) is None
