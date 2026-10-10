"""A durable cancellation receipt does not assert that the process already stopped."""

import pytest
from test_command_committed_receipts import IDENTITY, _attributed_command, _backend

from terminal_mcp.mcp.output_contracts import cmd_result


@pytest.mark.parametrize("initial_status", ["queued", "running"])
@pytest.mark.parametrize("failure", [None, "enqueue", "guard_exit"])
async def test_cancel_wire_preserves_observed_command_status(
    tmp_path, monkeypatch, initial_status, failure
):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend, status=initial_status)
    if failure == "enqueue":
        backend.terminal.enqueue_cancel.side_effect = RuntimeError("lost wakeup")
    elif failure == "guard_exit":
        backend.lifecycle.exit_error = RuntimeError("optional follow-up failed")

    accepted = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert accepted["ok"] is True
    stored = await backend.repo.get(command.cmd_hash)
    wire = cmd_result(accepted, "cancel").structuredContent
    assert wire["ok"] is True
    assert wire["command"]["cmd_hash"] == command.cmd_hash
    assert wire["command"]["status"] == stored.status
    assert stored.status == ("cancelled" if initial_status == "queued" else "running")


@pytest.mark.parametrize("final_status", ["cancelled", "completed", "failed"])
async def test_cancel_retry_preserves_observed_terminal_outcome(
    tmp_path, monkeypatch, final_status
):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend, status="running")
    accepted = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert accepted["ok"] is True
    await backend.repo.finish_running(command.cmd_hash, final_status)

    retried = await backend.cancel(command.cmd_hash, **IDENTITY)
    wire = cmd_result(retried, "cancel").structuredContent
    assert wire["ok"] is True
    assert wire["command"]["status"] == final_status
    assert wire["command"]["cancelled_from"] == "running"
    assert wire["command"]["execution_started"] is True
