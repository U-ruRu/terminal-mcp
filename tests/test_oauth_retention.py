import time

import aiosqlite
import pytest

from terminal_mcp.auth.storage import OAuthStore


async def counts(path):
    async with aiosqlite.connect(path) as db:
        codes = (await (await db.execute("SELECT count(*) FROM oauth_codes")).fetchone())[0]
        refresh = (
            await (await db.execute("SELECT count(*) FROM oauth_refresh_tokens")).fetchone()
        )[0]
        return codes, refresh


@pytest.mark.asyncio
async def test_initialize_purges_stale_oauth_rows_and_preserves_live_credentials(tmp_path):
    path = tmp_path / "oauth.sqlite3"
    store = OAuthStore(path)
    await store.initialize()
    now = int(time.time())
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            "INSERT INTO oauth_codes VALUES(?,?,?,?,?,?,?)",
            [
                ("expired", "c", "u", "s", "h", now - 1, 0),
                ("used", "c", "u", "s", "h", now + 100, 1),
                ("live", "c", "u", "s", "h", now + 100, 0),
            ],
        )
        await db.executemany(
            "INSERT INTO oauth_refresh_tokens VALUES(?,?,?,?,?)",
            [
                ("expired", "c", "s", now - 1, 0),
                ("revoked", "c", "s", now + 100, 1),
                ("live", "c", "s", now + 100, 0),
            ],
        )
        await db.commit()

    await store.initialize()
    assert await counts(path) == (1, 1)


@pytest.mark.asyncio
async def test_consume_code_removes_used_authorization_code(tmp_path):
    store = OAuthStore(tmp_path / "oauth.sqlite3")
    await store.initialize()
    code = await store.create_code("client", "https://cb", "scope", "challenge", 60)
    assert await store.consume_code(code) is True
    assert await store.get_code(code) is None
    assert (await counts(store.path))[0] == 0
