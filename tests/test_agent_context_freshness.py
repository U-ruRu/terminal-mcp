"""AgentCoordinator owns task-context freshness independently of runtime wiring."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.agents import AgentCoordinator
from terminal_mcp.core.orchestration import utc_text

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
AGENT = "Alpha-1111"


@pytest.fixture
def coordinator(monkeypatch):
    monkeypatch.setattr("terminal_mcp.core.agents.utc_now", lambda: NOW)
    return AgentCoordinator(SimpleNamespace(latest_task_at=AsyncMock()))


def test_context_ttl_uses_agent_policy():
    value = AgentCoordinator(object(), policy=AgentPolicy(intent_ttl_seconds=45))
    assert value.task_context_ttl_seconds == 45


@pytest.mark.parametrize("age", [179, 180])
async def test_context_is_fresh_through_exact_ttl(coordinator, age):
    coordinator.store.latest_task_at.return_value = utc_text(NOW - timedelta(seconds=age))

    assert await coordinator.validate_task_lease(AGENT) is None
    coordinator.store.latest_task_at.assert_awaited_once_with(AGENT)


async def test_context_expires_after_ttl(coordinator):
    coordinator.store.latest_task_at.return_value = utc_text(NOW - timedelta(seconds=181))

    rejected = await coordinator.validate_task_lease(AGENT)

    assert rejected["ok"] is False
    assert rejected["task_context_expired"] is True
    assert rejected["task_context_age_seconds"] == 181
    assert rejected["task_context_ttl_seconds"] == 180
    coordinator.store.latest_task_at.assert_awaited_once_with(AGENT)


async def test_missing_context_is_expired_without_an_invented_age(coordinator):
    coordinator.store.latest_task_at.return_value = None

    rejected = await coordinator.validate_task_lease(AGENT)

    assert rejected["ok"] is False
    assert rejected["task_context_expired"] is True
    assert rejected["task_context_age_seconds"] is None
    coordinator.store.latest_task_at.assert_awaited_once_with(AGENT)
