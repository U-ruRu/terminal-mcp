import asyncio
import sqlite3

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "runtime.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        oauth_issuer="https://terminal.example",
        oauth_audience="https://terminal.example/mcp",
        oauth_signing_secret="test-secret-that-is-at-least-32-bytes",
        oauth_required_scopes="terminal:read",
        oauth_access_ttl_sec=900,
        oauth_refresh_ttl_sec=3600,
        mcp_auth_mode="oauth",
        actions_auth_mode="oauth",
    )


def pair(app, client, label):
    secret = asyncio.run(app.state.pairing_store.create())
    response = client.post(
        "/pairing/exchange",
        json={
            "secret": secret,
            "device_label": label,
            "public_key": "public-key-material-" + ("x" * 64),
        },
    )
    assert response.status_code == 200
    return response.json()


def bearer(paired):
    return {"Authorization": f"Bearer {paired['access_token']}"}


def test_first_owner_bootstrap_is_paired_atomic_and_does_not_persist_password(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(app, client, "Owner phone")
        assert client.get("/access/bootstrap/status").json() == {"status": "uninitialized"}

        response = client.post(
            "/access/bootstrap/owner",
            headers=bearer(paired),
            json={
                "username": "owner@example.test",
                "display_name": "Owner",
                "password": "correct horse battery staple",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["principal"]["display_name"] == "Owner"
        assert body["client"]["client_id"] == paired["client_id"]
        assert body["grant"]["role"] == "owner"
        assert "auth:manage" in body["grant"]["scopes"]
        assert "password" not in response.text.lower()

        duplicate = client.post(
            "/access/bootstrap/owner",
            headers=bearer(paired),
            json={"username": "other", "password": "another-password"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "already_initialized"

        me = client.get("/access/me", headers=bearer(paired))
        assert me.status_code == 200
        assert me.json()["principal_id"] == body["principal"]["principal_id"]
        assert me.json()["client_id"] == paired["client_id"]
        assert me.json()["roles"] == ["owner"]

    raw = (tmp_path / "auth.sqlite3").read_bytes()
    assert b"correct horse battery staple" not in raw
    with sqlite3.connect(tmp_path / "auth.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM auth_realms").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM auth_principals").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM auth_clients").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM auth_grants").fetchone()[0] == 1


def test_invite_reset_and_independent_client_revoke_are_single_use(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        owner_device = pair(app, client, "Owner phone")
        owner = client.post(
            "/access/bootstrap/owner",
            headers=bearer(owner_device),
            json={"username": "owner", "display_name": "Owner", "password": "owner-password"},
        ).json()
        owner_headers = bearer(owner_device)

        invite = client.post(
            "/access/enrollments",
            headers=owner_headers,
            json={"purpose": "human_invite", "display_name": "Bob"},
        )
        assert invite.status_code == 200
        secret = invite.json()["secret"]
        exchanged = client.post(
            "/access/enrollments/exchange",
            json={
                "secret": secret,
                "username": "bob",
                "display_name": "Bob",
                "password": "bob-password",
            },
        )
        assert exchanged.status_code == 200
        bob_id = exchanged.json()["principal"]["principal_id"]
        assert (
            client.post(
                "/access/enrollments/exchange",
                json={"secret": secret, "username": "bob2", "password": "another-password"},
            ).status_code
            == 400
        )

        reset = client.post(
            "/access/recovery",
            headers=owner_headers,
            json={"purpose": "password_reset", "target_principal_id": bob_id},
        )
        assert reset.status_code == 200
        reset_secret = reset.json()["secret"]
        assert (
            client.post(
                "/access/recovery/exchange",
                json={"secret": reset_secret, "password": "bob-new-password"},
            ).status_code
            == 200
        )
        assert asyncio.run(app.state.auth_foundation.verify_password("bob", "bob-password")) is None
        assert asyncio.run(app.state.auth_foundation.verify_password("bob", "bob-new-password"))

        bob_device = pair(app, client, "Bob phone")
        assigned = client.post(
            f"/access/clients/{bob_device['client_id']}/assign",
            headers=owner_headers,
            json={"principal_id": bob_id, "role": "reader", "scopes": ["terminal:read"]},
        )
        assert assigned.status_code == 200
        bob_headers = bearer(bob_device)
        assert client.get("/access/me", headers=bob_headers).status_code == 200
        for admin_path in (
            "/access/principals",
            "/access/clients",
            "/access/grants",
            "/access/audit",
        ):
            forbidden = client.get(admin_path, headers=bob_headers)
            assert forbidden.status_code == 403
            assert forbidden.json()["error"] == "insufficient_access_scope"

        preview = client.post(
            "/access/revocations/preview",
            headers=owner_headers,
            json={"client_id": bob_device["client_id"]},
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert [item["client_id"] for item in preview_body["clients"]] == [bob_device["client_id"]]

        revoked = client.post(
            "/access/revocations/commit",
            headers=owner_headers,
            json={
                "client_id": bob_device["client_id"],
                "expected_generation": preview_body["security_generation"],
            },
        )
        assert revoked.status_code == 200
        assert client.get("/access/me", headers=bob_headers).status_code == 401
        assert client.get("/access/me", headers=owner_headers).status_code == 200
        assert asyncio.run(app.state.auth_foundation.verify_password("owner", "owner-password"))

        stale = client.post(
            "/access/revocations/commit",
            headers=owner_headers,
            json={
                "grant_id": owner["grant"]["grant_id"],
                "expected_generation": preview_body["security_generation"],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "stale_security_generation"



def test_last_auth_manager_cannot_self_revoke_without_replacement(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        owner_device = pair(app, client, "Owner phone")
        owner = client.post(
            "/access/bootstrap/owner",
            headers=bearer(owner_device),
            json={"username": "owner", "password": "owner-password"},
        ).json()
        owner_headers = bearer(owner_device)

        preview = client.post(
            "/access/revocations/preview",
            headers=owner_headers,
            json={"client_id": owner_device["client_id"]},
        ).json()
        blocked = client.post(
            "/access/revocations/commit",
            headers=owner_headers,
            json={
                "client_id": owner_device["client_id"],
                "expected_generation": preview["security_generation"],
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "last_auth_manager_required"
        assert client.get("/access/me", headers=owner_headers).status_code == 200
        assert client.get("/access/principals", headers=owner_headers).status_code == 200

        invite = client.post(
            "/access/enrollments",
            headers=owner_headers,
            json={"purpose": "human_invite", "display_name": "Backup manager"},
        ).json()
        backup_principal = client.post(
            "/access/enrollments/exchange",
            json={
                "secret": invite["secret"],
                "username": "backup-manager",
                "password": "backup-manager-password",
            },
        ).json()["principal"]
        backup_device = pair(app, client, "Backup phone")
        assigned = client.post(
            f"/access/clients/{backup_device['client_id']}/assign",
            headers=owner_headers,
            json={
                "principal_id": backup_principal["principal_id"],
                "role": "owner",
                "scopes": ["auth:manage", "terminal:read"],
            },
        )
        assert assigned.status_code == 200

        preview = client.post(
            "/access/revocations/preview",
            headers=owner_headers,
            json={"client_id": owner_device["client_id"]},
        ).json()
        revoked = client.post(
            "/access/revocations/commit",
            headers=owner_headers,
            json={
                "client_id": owner_device["client_id"],
                "expected_generation": preview["security_generation"],
            },
        )
        assert revoked.status_code == 200
        assert client.get("/access/me", headers=owner_headers).status_code == 401
        backup_headers = bearer(backup_device)
        assert client.get("/access/principals", headers=backup_headers).status_code == 200
        assert owner["principal"]["principal_id"] != backup_principal["principal_id"]
