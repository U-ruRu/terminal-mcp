"""Independent task-scope admission and command-attribution invariants."""

import sqlite3

import pytest
from test_v010 import register, runtime, wait_done


@pytest.mark.asyncio
async def test_run_without_explicit_task_scope_creates_no_command(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    await terminal.start()
    try:
        actor = await register(service, "scope admission")
        result = await service.run("printf should-not-run", agent_id=actor)
        assert result["ok"] is False
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_scope_none_does_not_attribute_command_to_claimed_task(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    await terminal.start()
    try:
        actor = await register(service, "scope none")
        key = {"namespace": "scope", "task_id": "ONE"}
        assert (
            await service.task(actor, action="create", **key, title="Task", isolation_hint="none")
        )["ok"]
        assert (await service.task(actor, action="claim", **key, claim_intent="Own task"))["ok"]
        command = await service.run("printf scoped-none", agent_id=actor, task_scope="none")
        assert command["ok"]
        await wait_done(service, command["cmd_hash"], actor)
        events = (await service.tasks(**key, show_details=True))["task"]["events"]
        assert not any(
            event["event_type"] == "command"
            and event["payload"].get("command_hash") == command["cmd_hash"]
            for event in events
        )
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_scope_all_attributes_command_to_owned_task(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    await terminal.start()
    try:
        actor = await register(service, "scope all")
        key = {"namespace": "scope", "task_id": "ONE"}
        assert (
            await service.task(actor, action="create", **key, title="Task", isolation_hint="none")
        )["ok"]
        assert (await service.task(actor, action="claim", **key, claim_intent="Own task"))["ok"]
        command = await service.run("printf scoped-all", agent_id=actor, task_scope="all")
        assert command["ok"]
        await wait_done(service, command["cmd_hash"], actor)
        events = (await service.tasks(**key, show_details=True))["task"]["events"]
        assert any(
            event["event_type"] == "command"
            and event["payload"].get("command_hash") == command["cmd_hash"]
            for event in events
        )
    finally:
        await terminal.stop()
