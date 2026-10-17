"""The canonical message entry point shares the native Access implementation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_application_boundary import actor

from terminal_mcp.application import TerminalApplication


@pytest.mark.asyncio
async def test_application_message_forwards_current_actor_and_native_options():
    message = AsyncMock(return_value={"ok": True, "action": "send"})
    app = TerminalApplication(
        SimpleNamespace(access_mesh_messages=SimpleNamespace(message=message))
    )
    current = actor()
    result = await app.message(current, text="hello", scope="local", mode="notify")

    assert result == {"ok": True, "action": "send"}
    message.assert_awaited_once_with(current, text="hello", scope="local", mode="notify")
