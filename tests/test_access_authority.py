"""Protected installation key boundaries survive retirement of old code issuers."""

import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.auth.foundation import AuthFoundationStore


@pytest_asyncio.fixture
async def access(tmp_path):
    store = AuthFoundationStore(tmp_path / "auth.sqlite3")
    await store.initialize()
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TABLE auth_access_slots(logical_agent_id TEXT PRIMARY KEY,public_name TEXT,"
            "slot_kind TEXT,authority_node_id TEXT,created_at TEXT,updated_at TEXT)"
        )
        db.execute(
            "INSERT INTO auth_access_slots"
            "(logical_agent_id,public_name,slot_kind,authority_node_id,"
            "created_at,updated_at) VALUES('la_one','Archive','persistent','secondary','old','old')"
        )
    return store


@pytest.mark.asyncio
async def test_missing_dedicated_key_refuses_current_access_data(access):
    access.path.with_name(access.path.name + ".access-key").unlink()

    with pytest.raises(Exception, match="key is missing"):
        await AuthFoundationStore(access.path).initialize()


@pytest.mark.asyncio
async def test_corrupt_dedicated_key_refuses_reopen(access):
    key = access.path.with_name(access.path.name + ".access-key")
    key.write_bytes(b"corrupt")
    key.chmod(0o600)

    with pytest.raises(Exception, match="key is corrupt"):
        await AuthFoundationStore(access.path).initialize()
