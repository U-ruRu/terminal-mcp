import asyncio
import sqlite3

import pytest

from terminal_mcp.auth.foundation import AuthConflictError, AuthFoundationStore


def run(coro):
    return asyncio.run(coro)


def test_access_codes_are_keyed_rotated_tombstoned_and_never_persisted_raw(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        store = AuthFoundationStore(path)
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
        store = AuthFoundationStore(tmp_path / "auth.sqlite3")
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
    assert replacement["public_name"] == "Alpha"


def test_auth_foundation_fails_closed_on_newer_schema(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=999")
        store = AuthFoundationStore(path)
        with pytest.raises(Exception, match="newer than supported"):
            await store.initialize()

    run(scenario())


def test_access_key_is_dedicated_durable_and_missing_key_fails_closed(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        store = AuthFoundationStore(path)
        await store.initialize()
        key_path = path.with_name(path.name + ".access-key")
        assert key_path.exists()
        assert (key_path.stat().st_mode & 0o777) == 0o600
        await store.register_access_slot("la_keyed", "secondary")
        issued = await store.issue_access_code("la_keyed", requested_code="ABCD")
        assert (await store.resolve_access_code(issued["access_code"]))[
            "logical_agent_id"
        ] == "la_keyed"
        key_path.unlink()
        restarted = AuthFoundationStore(path)
        with pytest.raises(Exception, match="key is missing"):
            await restarted.initialize()

    run(scenario())


def test_access_key_corruption_fails_closed_even_without_transport_secret_rotation(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        store = AuthFoundationStore(path)
        await store.initialize()
        key_path = path.with_name(path.name + ".access-key")
        key_path.write_bytes(b"corrupt")
        key_path.chmod(0o600)
        restarted = AuthFoundationStore(path)
        with pytest.raises(Exception, match="key is corrupt"):
            await restarted.initialize()

    run(scenario())
