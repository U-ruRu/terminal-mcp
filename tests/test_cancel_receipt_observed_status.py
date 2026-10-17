"""A durable cancellation receipt does not assert that the process already stopped."""

import pytest
from test_command_committed_receipts import IDENTITY, _attributed_command, _backend

from terminal_mcp.mcp.output_contracts import cmd_result


@pytest.mark.parametrize("initial_status", ["queued", "running"])
async def test_cancel_wire_preserves_observed_command_status(tmp_path, monkeypatch, initial_status):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend, status=initial_status)
    accepted = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert accepted["ok"] is True
    stored = await backend.repo.get(command.cmd_hash)
    wire = cmd_result(accepted, "cancel").structuredContent
    assert wire["ok"] is True
    assert wire["command"]["cmd_hash"] == command.cmd_hash
    assert wire["command"]["status"] == stored.status
    assert stored.status == ("cancelled" if initial_status == "queued" else "running")


async def test_cancel_retry_preserves_observed_terminal_outcome(tmp_path, monkeypatch):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend, status="running")
    await backend.repo.accept_cancel_intent(command.cmd_hash)
    await backend.repo.finish_running(command.cmd_hash, "completed")

    retried = await backend.cancel(command.cmd_hash, **IDENTITY)
    wire = cmd_result(retried, "cancel").structuredContent
    assert wire["ok"] is True
    assert wire["command"]["status"] == "completed"
    assert wire["command"]["cancelled_from"] == "running"
    assert wire["command"]["execution_started"] is True
