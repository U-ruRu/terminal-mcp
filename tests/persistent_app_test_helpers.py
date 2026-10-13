"""A forbidden native neighbor for the listed persistent application wiring checks."""

from unittest.mock import patch

import pytest
from test_execution_port import MemoryExecutionPort

from terminal_mcp.app import create_app


class NoNativeExecutionPort(MemoryExecutionPort):
    async def spawn(self, request):
        pytest.fail("application wiring must not spawn a native process")

    async def capture(self, *args, **kwargs):
        pytest.fail("application wiring must not capture a native process")


def persistent_app_with_execution_port(settings):
    with patch("terminal_mcp.app.build_execution", return_value=NoNativeExecutionPort()):
        return create_app(settings)
