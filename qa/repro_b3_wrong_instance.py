"""Reproducer for B3 authority provenance break on frozen 16b9d4d.
Synthetic isolated fixtures only; outputs are boolean summaries and digests.
"""
import asyncio
import hashlib
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from test_identity_migration import seed, settings
from terminal_mcp.app import create_app
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import (
    assert_identity_isolated, clean_working_data, inspect, migrate,
)
from terminal_mcp.auth.storage import OAuthStore


def run(coro):
    return asyncio.run(coro)


def fingerprint(path, table):
    with closing(sqlite3.connect(path)) as db:
        rows = sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def check():
    with tempfile.TemporaryDirectory(prefix="tmcp-delta3-provenance-") as d:
        root = Path(d)
        old = root / "existing"
        old.mkdir()
        main, auth, client_id, old_refresh, device, *_ = seed(old)
        foundation = AuthFoundationStore(auth)
        principal = run(foundation.create_principal("delta3-existing-user", "synthetic-password"))
        foundation_client = run(foundation.create_client(principal["principal_id"], "agent", "delta3-existing-agent"))
        grant = run(foundation.create_grant(
            principal["principal_id"], foundation_client["client_id"],
            ["https://synthetic.example"], ["terminal:read"], role="qa",
        ))
        run(foundation.bootstrap_realm("old-realm", "old-node", "old-signing", "old-public-key"))
        assert migrate(main, auth, old / "forward-backup")["status"] == "migrated"
        names = (
            "auth_principals", "auth_clients", "auth_grants", "auth_realms",
            "oauth_clients", "oauth_refresh_tokens", "console_devices", "console_pairing_audit",
        )
        before = {name: fingerprint(auth, name) for name in names}
        original_key_digest = hashlib.sha256(Path(str(auth) + ".access-key").read_bytes()).hexdigest()
        print("PRECONDITION original OAuth registration, Android device, principal, grant, realm: TRUE")
        fresh = root / "fresh-other-node"
        fresh.mkdir()
        fresh_main, fresh_auth = fresh / "runtime.sqlite3", fresh / "auth.sqlite3"
        with TestClient(create_app(settings(fresh, fresh_main, fresh_auth))) as client:
            assert client.get("/health/live").status_code == 200
        with closing(sqlite3.connect(fresh_auth)) as db:
            print("NEW_STORE schema_valid_and_marker_complete", db.execute(
                "SELECT version FROM oauth_identity_migration"
            ).fetchall() == [(1,)])
        shutil.copy2(fresh_auth, auth)
        after = {name: fingerprint(auth, name) for name in names}
        lost = [name for name in names if before[name] != after[name]]
        retained_key = original_key_digest == hashlib.sha256(Path(str(auth) + ".access-key").read_bytes()).hexdigest()
        print("AFTER_REPLACEMENT original_verifier_key_unchanged", retained_key)
        print("AFTER_REPLACEMENT lost_relation_count", len(lost))
        print("AFTER_REPLACEMENT original_principal_exists",
              bool(run(AuthFoundationStore(auth).verify_password("delta3-existing-user", "synthetic-password"))))
        print("AFTER_REPLACEMENT original_grant_exists", bool(
            run(AuthFoundationStore(auth).active_grants(principal["principal_id"]))
        ))
        print("AFTER_REPLACEMENT original_oauth_client_exists",
              run(OAuthStore(auth).get_client(client_id)) is not None)
        print("AFTER_REPLACEMENT original_refresh_still_valid",
              run(OAuthStore(auth).rotate_refresh(old_refresh)) is not None)
        with closing(sqlite3.connect(auth)) as db:
            print("AFTER_REPLACEMENT original_device_exists", db.execute(
                "SELECT COUNT(*) FROM console_devices WHERE device_id=?", (device.device_id,)
            ).fetchone()[0] == 1)
        print("GUARD allowed", assert_identity_isolated(main, auth) is None)
        print("INSPECT status", inspect(main, auth)["status"])
        with TestClient(create_app(settings(old, main, auth))) as client:
            print("STARTUP health_200", client.get("/health/live").status_code == 200)
        result = clean_working_data(main, auth, old / "output.sqlite3", old / "quarantine")
        print("CLEAN accepted", result["status"] == "cleaned")
        print("CLEAN removed_main", not main.exists())
        assert retained_key and len(lost) >= 5
        assert result["status"] == "cleaned" and not main.exists()


if __name__ == "__main__":
    check()
