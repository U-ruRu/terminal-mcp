import asyncio
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import public_agent_name, utc_now, utc_text
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def register(service, summary):
    started = await service.agent_start(
        task_summary=summary,
        intent=summary,
        details=[summary],
        work_scope=[f"test:{summary}"],
    )
    return started["self"]["agent_id"]


@pytest.mark.asyncio
async def test_expired_owner_stops_projecting_before_persisted_claim_cleanup(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "owner")
        peer = await register(service, "peer")
        created = await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="live",
            task_id="COOP",
            title="live owner projection",
            cooperative=True,
        )
        assert created["ok"] is True
        for agent, intent in ((owner, "first owner"), (peer, "next owner")):
            claimed = await service.task(
                agent,
                action="claim",
                namespace="live",
                task_id="COOP",
                claim_intent=intent,
            )
            assert claimed["ok"] is True

        old = utc_text(utc_now() - timedelta(seconds=301))
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?",
                (old, owner),
            )
            db.commit()

        persisted = await service.task_store.active_claims("live", "COOP")
        assert [item["agent_id"] for item in persisted] == [owner, peer]

        health = await service.health("none")
        workflow = health["workflow"]
        assert workflow["active_claims"] == 2  # compatibility alias for unreleased rows
        assert workflow["unreleased_claims"] == 2
        assert workflow["live_claims"] == 1
        assert workflow["stale_claims"] == 1

        detail = await service.tasks(namespace="live", task_id="COOP", show_details=True)
        assert [item["agent_name"] for item in detail["task"]["claims"]] == [
            public_agent_name(peer)
        ]
        assert detail["task"]["owner"]["agent_name"] == public_agent_name(peer)
        assert detail["task"]["participants"] == []

        fleet = await service.agents(peer)
        refs = {item["task_id"]: item for item in fleet["self"]["managed_tasks"]}
        assert refs["COOP"]["role"] == "owner"
        assert await service.task_coordinator.task_refs_for_agent(owner) == []

        checkpoint = await service.task(
            peer,
            action="checkpoint",
            namespace="live",
            task_id="COOP",
            checkpoint={"owner": "peer"},
        )
        assert checkpoint["ok"] is True
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_context_timer_has_canonical_name_and_compatibility_alias(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        started = await service.agent_start(
            task_summary="timer",
            intent="timer",
            details=["timer"],
            work_scope=["test:timer"],
        )
        agent = started["self"]["agent_id"]
        assert started["self"]["task_context_ttl_seconds"] == 180
        assert started["self"]["task_lease_seconds"] == 180

        service.agent_coordinator.task_lease_seconds = 0.01
        assert service.agent_coordinator.task_context_ttl_seconds == 0.01
        await asyncio.sleep(0.03)
        expired = await service.agent_coordinator.validate_task_lease(agent)
        assert expired["task_context_expired"] is True
        assert expired["task_context_ttl_seconds"] == 0.01
        assert expired["max_task_age_seconds"] == 0.01
        assert expired["task_context_age_seconds"] == expired["task_age_seconds"]
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_operational_status_is_derived_from_live_claim_lifecycle(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "status-owner")
        created = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status",
            task_id="WORK",
            title="derived status",
        )
        assert created["task"]["operational_status"] == "ready"

        claimed = await service.task(
            agent,
            action="claim",
            namespace="status",
            task_id="WORK",
            claim_intent="work the task",
        )
        assert claimed["task"]["state"] == "ready"
        assert claimed["task"]["operational_status"] == "in_progress"

        blocked = await service.task(
            agent,
            action="state",
            namespace="status",
            task_id="WORK",
            state="blocked",
            blocker_reason="temporary blocker",
        )
        assert blocked["task"]["operational_status"] == "blocked"
        fleet = await service.agents(agent)
        ref = next(item for item in fleet["self"]["managed_tasks"] if item["task_id"] == "WORK")
        assert ref["operational_status"] == "blocked"

        resumed = await service.task(
            agent, action="state", namespace="status", task_id="WORK", state="ready"
        )
        assert resumed["task"]["operational_status"] == "in_progress"

        filtered = await service.tasks(operational_status="in_progress")
        assert [item["task_id"] for item in filtered["tasks"]] == ["WORK"]
        assert filtered["summary"]["by_operational_status"] == {"in_progress": 1}

        released = await service.task(
            agent,
            action="release",
            namespace="status",
            task_id="WORK",
            release_reason="handoff",
        )
        assert released["task"]["operational_status"] == "ready"

        await service.task(
            agent,
            action="claim",
            namespace="status",
            task_id="WORK",
            claim_intent="finish session",
        )
        assert (await service.agent_finish(agent))["ok"] is True
        after_finish = await service.tasks(namespace="status", task_id="WORK")
        assert after_finish["task"]["operational_status"] == "ready"

        expirer = await register(service, "status-expirer")
        await service.task(
            expirer,
            action="claim",
            namespace="status",
            task_id="WORK",
            claim_intent="expire session",
        )
        old = utc_text(utc_now() - timedelta(seconds=301))
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?",
                (old, expirer),
            )
            db.commit()
        persisted = await service.task_store.active_claims("status", "WORK")
        assert [item["agent_id"] for item in persisted] == [expirer]
        after_expiry = await service.tasks(namespace="status", task_id="WORK")
        assert after_expiry["task"]["operational_status"] == "ready"
        ready = await service.tasks(operational_status="ready")
        assert ready["recommended"]["task_id"] == "WORK"
        assert ready["recommended"]["operational_status"] == "ready"
    finally:
        await terminal.stop()
