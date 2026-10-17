"""Current command attribution at the backend-to-task coordinator port."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_command_committed_receipts import IDENTITY, _backend


async def test_scope_all_records_current_session_on_owned_tasks(tmp_path, monkeypatch):
    backend = await _backend(tmp_path, monkeypatch)
    selected = [{"namespace": "scope", "task_id": "ONE"}]
    backend._task_refs = AsyncMock(return_value=selected)
    record = AsyncMock()
    backend.task_coordinator = SimpleNamespace(record_command=record)

    command = await backend.run("printf scoped", queue_id=1, task_scope="all", **IDENTITY)

    assert command["ok"] is True
    assert command["task_targets"] == ["scope/ONE"]
    record.assert_awaited_once_with(
        IDENTITY["logical_agent_id"], command["cmd_hash"], "persistent_run", selected, **IDENTITY
    )
