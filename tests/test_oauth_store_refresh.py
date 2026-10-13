"""OAuthStore owns atomic single-use refresh rotation in its own SQLite database."""

import asyncio

from terminal_mcp.auth.storage import OAuthStore


def test_refresh_rotation_is_single_use_under_concurrency(tmp_path):
    async def scenario():
        store = OAuthStore(tmp_path / "refresh-race.sqlite3")
        await store.initialize()
        client_id, _ = await store.register_client(
            ["https://client.example/callback"], "Concurrent client", "none"
        )
        token = await store.create_refresh(client_id, "terminal:read", 3600)
        results = await asyncio.gather(*(store.rotate_refresh(token) for _ in range(20)))
        return results

    results = asyncio.run(scenario())
    assert sum(result is not None for result in results) == 1
