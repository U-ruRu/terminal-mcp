"""Atomic responsibilities of the current authorization store."""

import pytest
import pytest_asyncio

from terminal_mcp.auth.foundation import AuthConflictError, AuthFoundationStore


@pytest_asyncio.fixture
async def store(tmp_path):
    value = AuthFoundationStore(tmp_path / "auth.sqlite3")
    await value.initialize()
    return value


async def test_password_verifier_authenticates_owner(store):
    await store.create_principal("owner@example.test", "correct password", principal_id="usr_owner")
    principal = await store.verify_password("owner@example.test", "correct password")
    assert principal["principal_id"] == "usr_owner"


async def test_grant_revocation_keeps_another_users_grant(store):
    await store.bootstrap_realm("realm", "node", "key", "public-key")
    for name in ("alice", "bob"):
        await store.create_principal(name, "password", principal_id=name)
        await store.create_client(name, "connector", name, client_id="cli_" + name)
        await store.create_grant(
            name, "cli_" + name, ["node:a"], ["terminal:read"], grant_id="grt_" + name
        )
    await store.revoke_grant("grt_alice", actor_principal_id="alice")
    assert await store.active_grants("alice") == []
    assert [g["grant_id"] for g in await store.active_grants("bob")] == ["grt_bob"]


async def test_grant_rejects_another_principals_client(store):
    await store.create_principal("alice", "password", principal_id="alice")
    await store.create_principal("bob", "password", principal_id="bob")
    await store.create_client("alice", "app", "Alice app", client_id="cli_alice")
    with pytest.raises(AuthConflictError, match="different principal"):
        await store.create_grant("bob", "cli_alice", ["node:a"], ["terminal:read"])


async def test_provider_binding_records_its_logical_agent(store):
    await store.register_access_slot("la_one", "secondary")
    bound = await store.bind_provider_binding("openai", "a" * 64, "la_one", principal_id="usr")
    assert bound == "la_one"
    assert await store.resolve_provider_binding("openai", "a" * 64) == "la_one"


async def test_provider_binding_rejects_a_second_agent_for_one_conversation(store):
    await store.register_access_slot("la_one", "secondary")
    await store.register_access_slot("la_two", "secondary")
    await store.bind_provider_binding("openai", "a" * 64, "la_one", principal_id="usr")
    with pytest.raises(AuthConflictError, match="already bound"):
        await store.bind_provider_binding("openai", "a" * 64, "la_two", principal_id="usr")


async def test_provider_binding_rejects_a_second_conversation_for_one_agent(store):
    await store.register_access_slot("la_one", "secondary")
    await store.bind_provider_binding("openai", "a" * 64, "la_one", principal_id="usr")
    with pytest.raises(AuthConflictError, match="slot is already bound"):
        await store.bind_provider_binding("openai", "b" * 64, "la_one", principal_id="usr")
