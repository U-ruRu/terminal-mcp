"""Current identity isolation. All credentials are synthetic."""

import shutil
import sqlite3
from contextlib import closing

import pytest

from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import (
    IdentityLineageError,
    assert_identity_isolated,
    initialize_fresh_identity,
)
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.storage import OAuthStore


async def test_fresh_identity_seal_records_installation(tmp_path):
    runtime, auth = tmp_path / "runtime.sqlite3", tmp_path / "auth.sqlite3"
    await AuthFoundationStore(auth).initialize()
    await OAuthStore(auth).initialize()
    await PairingStore(auth).initialize()

    initialize_fresh_identity(runtime, auth)

    with closing(sqlite3.connect(auth)) as db:
        assert db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(1,)]
    assert assert_identity_isolated(runtime, auth) is None


async def test_current_identity_rejects_missing_installation_anchor(tmp_path):
    runtime, auth = tmp_path / "runtime.sqlite3", tmp_path / "auth.sqlite3"
    await AuthFoundationStore(auth).initialize()
    await OAuthStore(auth).initialize()
    await PairingStore(auth).initialize()
    initialize_fresh_identity(runtime, auth)
    auth.with_name(auth.name + ".identity-anchor").unlink()

    with pytest.raises(IdentityLineageError, match="anchor is missing"):
        assert_identity_isolated(runtime, auth)


async def test_current_identity_rejects_foreign_database(tmp_path):
    runtime = tmp_path / "runtime.sqlite3"
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    for auth in (first, second):
        await AuthFoundationStore(auth).initialize()
        await OAuthStore(auth).initialize()
        await PairingStore(auth).initialize()
        initialize_fresh_identity(runtime, auth)
    shutil.copy2(first, second)

    with pytest.raises(IdentityLineageError):
        assert_identity_isolated(runtime, second)
