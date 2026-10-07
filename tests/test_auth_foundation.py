import asyncio
import sqlite3

import pytest

from terminal_mcp.auth.foundation import AuthConflictError, AuthFoundationStore


def run(coro):
    return asyncio.run(coro)


def test_password_verifier_is_argon2id_and_plaintext_never_persisted(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        store = AuthFoundationStore(path)
        await store.initialize()
        principal = await store.create_principal(
            "Owner@example.test",
            "correct horse battery staple",
            display_name="Owner",
            principal_id="usr_owner",
        )
        assert await store.verify_password("owner@example.test", "correct horse battery staple")
        assert await store.verify_password("owner@example.test", "wrong") is None
        return path, principal

    path, principal = run(scenario())
    assert principal["principal_id"] == "usr_owner"
    with sqlite3.connect(path) as db:
        verifier = db.execute(
            "SELECT password_verifier FROM auth_principals WHERE principal_id='usr_owner'"
        ).fetchone()[0]
    assert verifier.startswith("$argon2id$")
    assert b"correct horse battery staple" not in path.read_bytes()


def test_authority_epoch_fences_stale_writer_and_db_has_no_private_key_column(tmp_path):
    async def scenario():
        path = tmp_path / "auth.sqlite3"
        store = AuthFoundationStore(path)
        await store.initialize()
        realm = await store.bootstrap_realm("realm_shared", "node_a", "key_1", "public-key-a")
        moved = await store.transfer_authority(
            "realm_shared", realm["authority_epoch"], "node_b", "key_2", "public-key-b"
        )
        with pytest.raises(AuthConflictError, match="stale auth authority epoch"):
            await store.transfer_authority(
                "realm_shared", realm["authority_epoch"], "node_c", "key_3", "public-key-c"
            )
        return path, moved, await store.security_generation()

    path, moved, generation = run(scenario())
    assert moved["authority_node_id"] == "node_b"
    assert moved["authority_epoch"] == 2
    assert generation == 2
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(auth_realms)")]
    assert "private_key" not in columns
    assert "private_signing_key" not in columns


def test_multi_user_client_grants_revoke_independently_with_attributed_audit(tmp_path):
    async def scenario():
        store = AuthFoundationStore(tmp_path / "auth.sqlite3")
        await store.initialize()
        await store.bootstrap_realm("realm_shared", "node_a", "key_1", "public-key-a")
        alice = await store.create_principal("alice", "alice-secret", principal_id="usr_alice")
        bob = await store.create_principal("bob", "bob-secret", principal_id="usr_bob")
        alice_client = await store.create_client(
            alice["principal_id"], "connector", "Alice ChatGPT", client_id="cli_alice"
        )
        bob_client = await store.create_client(
            bob["principal_id"], "device", "Bob phone", client_id="cli_bob"
        )
        alice_grant = await store.create_grant(
            alice["principal_id"], alice_client["client_id"], ["node:a"], ["terminal:read"],
            role="operator", grant_id="grt_alice"
        )
        bob_grant = await store.create_grant(
            bob["principal_id"], bob_client["client_id"], ["node:a"], ["terminal:read"],
            role="reader", grant_id="grt_bob"
        )
        before = await store.security_generation()
        assert await store.revoke_grant(alice_grant["grant_id"], actor_principal_id="usr_alice")
        after = await store.security_generation()
        return (
            await store.active_grants("usr_alice"),
            await store.active_grants("usr_bob"),
            bob_grant,
            before,
            after,
            await store.audit_events(),
        )

    alice_active, bob_active, bob_grant, before, after, audit = run(scenario())
    assert alice_active == []
    assert [grant["grant_id"] for grant in bob_active] == [bob_grant["grant_id"]]
    assert after == before + 1
    revoke = next(event for event in audit if event["event_type"] == "grant_revoke")
    assert revoke["principal_id"] == "usr_alice"
    assert revoke["client_id"] == "cli_alice"
    assert revoke["grant_id"] == "grt_alice"


def test_client_must_belong_to_same_principal_as_grant(tmp_path):
    async def scenario():
        store = AuthFoundationStore(tmp_path / "auth.sqlite3")
        await store.initialize()
        alice = await store.create_principal("alice", "secret-a", principal_id="usr_alice")
        bob = await store.create_principal("bob", "secret-b", principal_id="usr_bob")
        client = await store.create_client(
            alice["principal_id"], "app", "Alice app", client_id="cli_alice"
        )
        with pytest.raises(AuthConflictError, match="different principal"):
            await store.create_grant(
                bob["principal_id"], client["client_id"], ["node:a"], ["terminal:read"]
            )

    run(scenario())



def test_provider_binding_registry_is_canonical_and_conflict_safe(tmp_path):
    async def scenario():
        store = AuthFoundationStore(tmp_path / "auth.sqlite3")
        await store.initialize()
        await store.register_access_slot("la_one", "secondary")
        await store.register_access_slot("la_two", "secondary")
        key = "a" * 64
        assert await store.resolve_provider_binding("openai", key) is None
        bound = await store.bind_provider_binding(
            "openai", key, "la_one", principal_id="usr_one"
        )
        assert bound == "la_one"
        assert await store.resolve_provider_binding("openai", key) == "la_one"
        assert (
            await store.bind_provider_binding(
                "openai", key, "la_one", principal_id="usr_one"
            )
            == "la_one"
        )
        with pytest.raises(AuthConflictError, match="already bound"):
            await store.bind_provider_binding(
                "openai", key, "la_two", principal_id="usr_two"
            )
        events = await store.audit_events()
        assert any(e["event_type"] == "provider_binding_create" for e in events)

    run(scenario())


def test_one_provider_slot_cannot_be_shared_by_different_conversations(tmp_path):
    async def scenario():
        store = AuthFoundationStore(tmp_path / "auth-provider-unique.sqlite3")
        await store.initialize()
        await store.register_access_slot("la_one", "secondary")
        await store.bind_provider_binding("openai", "a" * 64, "la_one", principal_id="usr_one")
        with pytest.raises(AuthConflictError, match="slot is already bound"):
            await store.bind_provider_binding(
                "openai", "b" * 64, "la_one", principal_id="usr_one"
            )
        assert await store.resolve_provider_binding("openai", "a" * 64) == "la_one"
        assert await store.resolve_provider_binding("openai", "b" * 64) is None

    run(scenario())
