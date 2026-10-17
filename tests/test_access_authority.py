"""Current Access-code storage and dedicated-key recovery boundaries."""

import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.auth.foundation import AuthFoundationStore


@pytest_asyncio.fixture
async def access(tmp_path):
    store = AuthFoundationStore(tmp_path / "auth.sqlite3")
    await store.initialize()
    await store.register_access_slot("la_one", "secondary")
    return store


@pytest.mark.asyncio
async def test_issued_code_uses_distinct_keyed_index_and_verifier(access):
    await access.issue_access_code("la_one", requested_code="0042")

    with sqlite3.connect(access.path) as db:
        stored = db.execute("SELECT code_index,verifier FROM auth_access_codes").fetchone()
    assert stored == (access.access._index("0042"), access.access._verifier("0042"))
    assert len(stored[0]) == len(stored[1]) == 64
    assert stored[0] != stored[1]


@pytest.mark.asyncio
async def test_issued_code_preserves_leading_zeroes(access):
    await access.issue_access_code("la_one", requested_code="0042")

    resolved = await access.resolve_access_code("0042")

    assert resolved["logical_agent_id"] == "la_one"


@pytest.mark.asyncio
async def test_missing_dedicated_key_refuses_current_access_data(access):
    await access.issue_access_code("la_one", requested_code="0042")
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
