import asyncio
import sqlite3

import pytest

from terminal_mcp.auth.foundation import AuthConflictError, AuthFoundationStore


def run(coro):
    return asyncio.run(coro)


def test_access_codes_are_keyed_rotated_tombstoned_and_never_persisted_raw(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        store = AuthFoundationStore(path, access_code_secret="unit-access-secret")
        await store.initialize()
        await store.reserve_access_codes(["GRH8", "7X81"])
        slot = await store.register_access_slot("la_one", "secondary", display_suffix="One")
        first = await store.issue_access_code("la_one", requested_code="ABCD")
        assert (await store.resolve_access_code("ABCD"))["logical_agent_id"] == "la_one"
        second = await store.issue_access_code("la_one", requested_code="EFGH")
        assert await store.resolve_access_code("ABCD") is None
        assert (await store.resolve_access_code("EFGH"))["public_name"] == "Alpha"
        with pytest.raises(AuthConflictError):
            await store.issue_access_code("la_one", requested_code="GRH8")
        return path, slot, first, second

    path, slot, first, second = run(scenario())
    assert slot["public_name"] == "Alpha"
    assert first["access_generation"] == 1
    assert second["access_generation"] == 2
    raw = path.read_bytes()
    for plaintext in (b"ABCD", b"EFGH", b"GRH8", b"7X81"):
        assert plaintext not in raw
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM auth_access_codes WHERE tombstoned_at IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM auth_access_code_tombstones").fetchone()[0] == 2


def test_access_public_names_continue_after_nato_alphabet_without_reuse(tmp_path):
    async def scenario():
        store = AuthFoundationStore(
            tmp_path / "auth.sqlite3", access_code_secret="unit-access-secret"
        )
        await store.initialize()
        names = []
        for index in range(1, 29):
            row = await store.register_access_slot(f"la_{index}", "secondary")
            names.append(row["public_name"])
        await store.retire_access_slot("la_1")
        replacement = await store.register_access_slot("la_replacement", "secondary")
        return names, replacement

    names, replacement = run(scenario())
    assert len(set(names)) == 28
    assert names[0] == "Alpha"
    assert names[25] == "Zulu"
    assert names[26] == "Alpha-2"
    assert names[27] == "Bravo-2"
    assert replacement["public_name"] == "Charlie-2"


def test_auth_foundation_fails_closed_on_newer_schema(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=999")
        store = AuthFoundationStore(path, access_code_secret="unit-access-secret")
        with pytest.raises(Exception, match="newer than supported"):
            await store.initialize()

    run(scenario())
