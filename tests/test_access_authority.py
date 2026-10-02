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
        await store.reserve_access_codes(["1188", "7001"])
        slot = await store.register_access_slot("la_one", "secondary", display_suffix="One")
        first = await store.issue_access_code("la_one", requested_code="0042")
        assert (await store.resolve_access_code("0042"))["logical_agent_id"] == "la_one"
        second = await store.issue_access_code("la_one", requested_code="7319")
        assert await store.resolve_access_code("0042") is None
        assert (await store.resolve_access_code("7319"))["public_name"] == "Alpha"
        with pytest.raises(AuthConflictError):
            await store.issue_access_code("la_one", requested_code="1188")
        return path, slot, first, second

    path, slot, first, second = run(scenario())
    assert slot["public_name"] == "Alpha"
    assert first["access_generation"] == 1
    assert second["access_generation"] == 2
    raw = path.read_bytes()
    for plaintext in (b"0042", b"7319", b"1188", b"7001"):
        assert plaintext not in raw
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM auth_access_codes WHERE tombstoned_at IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM auth_access_code_tombstones").fetchone()[0] == 2


def test_access_codes_are_strict_four_digit_decimal_and_preserve_leading_zeroes(tmp_path):
    async def scenario():
        store = AuthFoundationStore(tmp_path / "auth.sqlite3")
        await store.initialize()
        await store.register_access_slot("la_numeric", "secondary")
        explicit = await store.issue_access_code("la_numeric", requested_code="0042")
        assert explicit["access_code"] == "0042"
        assert (await store.resolve_access_code("0042"))["logical_agent_id"] == "la_numeric"
        for invalid in ("ABCD", "12A4", "123", "12345", "１２３４"):
            with pytest.raises(ValueError, match="4 decimal digits"):
                await store.resolve_access_code(invalid)
        await store.register_access_slot("la_generated", "secondary")
        generated = await store.issue_access_code("la_generated")
        assert len(generated["access_code"]) == 4
        assert generated["access_code"].isascii()
        assert generated["access_code"].isdigit()

    run(scenario())


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
        issued = await store.issue_access_code("la_keyed", requested_code="0042")
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


def test_auth_v2_to_v3_migration_preserves_access_security_state(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE auth_access_slots(
                    logical_agent_id TEXT PRIMARY KEY,
                    public_name TEXT NOT NULL UNIQUE,
                    slot_kind TEXT NOT NULL CHECK(slot_kind IN ('persistent','legacy')),
                    display_suffix TEXT,
                    authority_node_id TEXT NOT NULL,
                    access_generation INTEGER NOT NULL DEFAULT 0 CHECK(access_generation >= 0),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE TABLE auth_access_codes(
                    code_index TEXT PRIMARY KEY,
                    logical_agent_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    verifier TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT,
                    tombstoned_at TEXT,
                    UNIQUE(logical_agent_id,generation),
                    FOREIGN KEY(logical_agent_id)
                        REFERENCES auth_access_slots(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE auth_access_code_tombstones(
                    code_index TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO auth_access_slots VALUES(
                    'la_deleted','Alpha','legacy',NULL,'secondary',1,'deleted',
                    '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z','2026-01-02T00:00:00Z'
                );
                INSERT INTO auth_access_slots VALUES(
                    'la_active','Bravo','persistent','Active','secondary',2,'active',
                    '2026-01-01T00:00:00Z','2026-01-03T00:00:00Z',NULL
                );
                INSERT INTO auth_access_codes VALUES(
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'la_active',2,
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                    '2026-01-03T00:00:00Z',NULL,NULL
                );
                INSERT INTO auth_access_code_tombstones VALUES(
                    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                    'rotated','2026-01-02T00:00:00Z'
                );
                PRAGMA user_version=2;
                """
            )

        store = AuthFoundationStore(path)
        # A v2 deployment did not own the dedicated v3 key yet. Provisioning the
        # rollback-excluded key is an explicit upgrade step before opening state.
        store.access.key_store.load_or_create(allow_create=True)
        await store.initialize()
        replacement = await store.register_access_slot(
            "la_replacement", "secondary", slot_kind="legacy"
        )

        with sqlite3.connect(path) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            slots = db.execute(
                "SELECT logical_agent_id,public_name,access_generation,status "
                "FROM auth_access_slots ORDER BY logical_agent_id"
            ).fetchall()
            codes = db.execute(
                "SELECT logical_agent_id,generation,code_index,verifier FROM auth_access_codes"
            ).fetchall()
            tombstones = db.execute(
                "SELECT code_index,reason FROM auth_access_code_tombstones"
            ).fetchall()
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO auth_access_slots("
                    "logical_agent_id,public_name,slot_kind,authority_node_id,"
                    "access_generation,status,created_at,updated_at"
                    ") VALUES(?,?,?,?,0,'active',?,?)",
                    (
                        "la_duplicate",
                        "Bravo",
                        "legacy",
                        "secondary",
                        "2026-01-04T00:00:00Z",
                        "2026-01-04T00:00:00Z",
                    ),
                )
        return version, violations, slots, codes, tombstones, replacement

    version, violations, slots, codes, tombstones, replacement = run(scenario())
    assert version == 3
    assert violations == []
    assert ("la_active", "Bravo", 2, "active") in slots
    assert ("la_deleted", "Alpha", 1, "deleted") in slots
    assert codes == [
        (
            "la_active",
            2,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
    ]
    assert tombstones == [
        (
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "rotated",
        )
    ]
    assert replacement["public_name"] == "Alpha"
