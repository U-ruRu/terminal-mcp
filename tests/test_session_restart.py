import sqlite3
from datetime import timedelta

import pytest

import terminal_mcp.core.agents as agents_module
from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path, *, policy=None):
    repo = SqliteRepository(tmp_path / "terminal.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, agent_policy=policy)
    return repo, terminal, service


async def create_session(
    repo,
    agent_id,
    *,
    started,
    last_activity,
    expires_at,
    source_instance_id=None,
):
    store = AgentStore(repo.path)
    await store.create_session(
        agent_id,
        "restart-safe task",
        "preserve absolute clock",
        ["repo:synthetic"],
        ["restart service", "verify clock"],
        1,
        last_activity,
        registered_at=started,
        source_instance_id=source_instance_id,
        global_expires_at=expires_at,
    )
    if last_activity != started:
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?",
                (last_activity, agent_id),
            )
            db.commit()
    return store


@pytest.mark.asyncio
async def test_restart_preserves_started_expiry_age_and_remaining(tmp_path, monkeypatch):
    policy = AgentPolicy(
        idle_ttl_seconds=900,
        max_session_seconds=1500,
        session_warning_after_seconds=1200,
    )
    repo, terminal, _ = await runtime(tmp_path, policy=policy)
    now = parse_utc(utc_text(utc_now()))
    started = utc_text(now - timedelta(seconds=600))
    last_activity = utc_text(now - timedelta(seconds=30))
    expires_at = utc_text(now + timedelta(seconds=900))
    agent_id = "Alpha-01234567"
    await create_session(
        repo,
        agent_id,
        started=started,
        last_activity=last_activity,
        expires_at=expires_at,
    )
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=policy)
    restart_now = now + timedelta(seconds=75)
    monkeypatch.setattr(agents_module, "utc_now", lambda: restart_now)
    result = await service2.agent_coordinator.reconcile_sessions(now=restart_now)
    context = await service2.agent_coordinator.session_context(agent_id)
    stored = await AgentStore(repo2.path).get_session(agent_id)

    assert result == {"examined": 1, "backfilled": 0, "expired": 0}
    assert stored["registered_at"] == started
    assert stored["last_activity_at"] == last_activity
    assert stored["global_expires_at"] == expires_at
    assert context["session_started_at"] == started
    assert context["session_age_seconds"] == 675
    assert context["session_remaining_seconds"] == 825
    await terminal2.stop()


@pytest.mark.asyncio
async def test_upgrade_backfills_legacy_deadline_once_and_restarts_cannot_extend_it(tmp_path):
    initial_policy = AgentPolicy(idle_ttl_seconds=5000, max_session_seconds=1380)
    repo, terminal, _ = await runtime(tmp_path, policy=initial_policy)
    now = parse_utc(utc_text(utc_now()))
    started_dt = now - timedelta(seconds=300)
    started = utc_text(started_dt)
    agent_id = "Bravo-89ABCDEF"
    await create_session(
        repo,
        agent_id,
        started=started,
        last_activity=utc_text(now - timedelta(seconds=10)),
        expires_at=None,
    )
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=initial_policy)
    first_restart = now + timedelta(seconds=30)
    first = await service2.agent_coordinator.reconcile_sessions(now=first_restart)
    fixed = await AgentStore(repo2.path).get_session(agent_id)
    expected_expiry = utc_text(started_dt + timedelta(seconds=1380))

    assert first["backfilled"] == 1
    assert fixed["global_expires_at"] == expected_expiry
    await terminal2.stop()

    changed_policy = AgentPolicy(idle_ttl_seconds=5000, max_session_seconds=3600)
    repo3, terminal3, service3 = await runtime(tmp_path, policy=changed_policy)
    second = await service3.agent_coordinator.reconcile_sessions(
        now=now + timedelta(seconds=60)
    )
    preserved = await AgentStore(repo3.path).get_session(agent_id)

    assert second["backfilled"] == 0
    assert preserved["global_expires_at"] == expected_expiry
    await terminal3.stop()


@pytest.mark.asyncio
async def test_restart_reconciles_idle_expiry_elapsed_during_downtime(tmp_path):
    policy = AgentPolicy(idle_ttl_seconds=300, max_session_seconds=1500)
    repo, terminal, _ = await runtime(tmp_path, policy=policy)
    now = parse_utc(utc_text(utc_now()))
    agent_id = "Charlie-01234567"
    await create_session(
        repo,
        agent_id,
        started=utc_text(now - timedelta(seconds=200)),
        last_activity=utc_text(now - timedelta(seconds=100)),
        expires_at=utc_text(now + timedelta(seconds=1300)),
    )
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=policy)
    restart_now = now + timedelta(seconds=250)
    result = await service2.agent_coordinator.reconcile_sessions(now=restart_now)
    stored = await AgentStore(repo2.path).get_session(agent_id)

    assert result["expired"] == 1
    assert stored["state"] == "forced"
    assert stored["end_reason"] == "idle_timeout"
    assert stored["ended_at"] == utc_text(restart_now)
    await terminal2.stop()


@pytest.mark.asyncio
async def test_restart_reconciles_hard_expiry_elapsed_during_downtime(tmp_path):
    policy = AgentPolicy(idle_ttl_seconds=5000, max_session_seconds=1500)
    repo, terminal, _ = await runtime(tmp_path, policy=policy)
    now = parse_utc(utc_text(utc_now()))
    agent_id = "Delta-89ABCDEF"
    started = utc_text(now - timedelta(seconds=1400))
    expires_at = utc_text(now + timedelta(seconds=100))
    await create_session(
        repo,
        agent_id,
        started=started,
        last_activity=utc_text(now - timedelta(seconds=5)),
        expires_at=expires_at,
    )
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=policy)
    restart_now = now + timedelta(seconds=150)
    result = await service2.agent_coordinator.reconcile_sessions(now=restart_now)
    stored = await AgentStore(repo2.path).get_session(agent_id)
    resumed = await service2.agent_start(agent_id=agent_id)

    assert result["expired"] == 1
    assert stored["state"] == "forced"
    assert stored["end_reason"] == "max_session_duration"
    assert stored["registered_at"] == started
    assert stored["global_expires_at"] == expires_at
    assert resumed["return_to_chat"] is True
    assert resumed["session_end_reason"] == "max_session_duration"
    await terminal2.stop()


@pytest.mark.asyncio
async def test_restart_warning_uses_original_absolute_start(tmp_path, monkeypatch):
    policy = AgentPolicy(
        idle_ttl_seconds=5000,
        max_session_seconds=1500,
        session_warning_after_seconds=1200,
    )
    repo, terminal, _ = await runtime(tmp_path, policy=policy)
    now = parse_utc(utc_text(utc_now()))
    agent_id = "Echo-01234567"
    started = utc_text(now - timedelta(seconds=1190))
    expires_at = utc_text(parse_utc(started) + timedelta(seconds=1500))
    await create_session(
        repo,
        agent_id,
        started=started,
        last_activity=utc_text(now - timedelta(seconds=5)),
        expires_at=expires_at,
    )
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=policy)
    restart_now = now + timedelta(seconds=20)
    monkeypatch.setattr(agents_module, "utc_now", lambda: restart_now)
    await service2.agent_coordinator.reconcile_sessions(now=restart_now)
    context = await service2.agent_coordinator.session_context(agent_id)

    assert context["session_age_seconds"] == 1210
    assert context["session_remaining_seconds"] == 290
    assert "safe checkpoint" in context["session_warning"]
    await terminal2.stop()


@pytest.mark.asyncio
async def test_foreign_attachment_and_repeated_restarts_keep_same_global_clock(
    tmp_path, monkeypatch
):
    policy = AgentPolicy(idle_ttl_seconds=5000, max_session_seconds=1500)
    repo, terminal, _ = await runtime(tmp_path, policy=policy)
    now = parse_utc(utc_text(utc_now()))
    agent_id = "Foxtrot-89ABCDEF"
    started = utc_text(now - timedelta(seconds=400))
    expires_at = utc_text(now + timedelta(seconds=1100))
    await create_session(
        repo,
        agent_id,
        started=started,
        last_activity=utc_text(now - timedelta(seconds=10)),
        expires_at=expires_at,
        source_instance_id="server-a",
    )
    await terminal.stop()

    for offset in (25, 80, 140):
        repo_n, terminal_n, service_n = await runtime(tmp_path, policy=policy)
        current = now + timedelta(seconds=offset)
        monkeypatch.setattr(agents_module, "utc_now", lambda current=current: current)
        result = await service_n.agent_coordinator.reconcile_sessions(now=current)
        context = await service_n.agent_coordinator.session_context(agent_id)
        stored = await AgentStore(repo_n.path).get_session(agent_id)

        assert result["backfilled"] == 0
        assert result["expired"] == 0
        assert stored["source_instance_id"] == "server-a"
        assert stored["registered_at"] == started
        assert stored["global_expires_at"] == expires_at
        assert context["session_started_at"] == started
        assert context["session_remaining_seconds"] == 1100 - offset
        await terminal_n.stop()


@pytest.mark.asyncio
async def test_restart_alert_schedule_uses_original_start_without_duplicate_reset(
    tmp_path, monkeypatch
):
    policy = AgentPolicy(
        idle_ttl_seconds=5000,
        max_session_seconds=100,
        session_warning_after_seconds=60,
        session_alert_enabled=True,
        session_alert_after_seconds=80,
        session_alert_repeat_seconds=10,
    )
    repo, terminal, _ = await runtime(tmp_path, policy=policy)
    now = parse_utc(utc_text(utc_now()))
    agent_id = "Golf-01234567"
    started = utc_text(now - timedelta(seconds=85))
    expires_at = utc_text(now + timedelta(seconds=15))
    await create_session(
        repo,
        agent_id,
        started=started,
        last_activity=utc_text(now - timedelta(seconds=1)),
        expires_at=expires_at,
    )
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=policy)
    monkeypatch.setattr(agents_module, "utc_now", lambda: now)
    await service2.agent_coordinator.reconcile_sessions(now=now)
    first_state = await service2.agent_coordinator.message_state(agent_id)
    first_journal = await AgentStore(repo2.path).message_journal(agent_id)
    first_alerts = [
        item for item in first_journal if item["sender_agent_id"] == "system-session"
    ]

    assert first_state["alert_pending"] is True
    assert len(first_alerts) == 1
    await terminal2.stop()

    repo3, terminal3, service3 = await runtime(tmp_path, policy=policy)
    second_now = now + timedelta(seconds=5)
    monkeypatch.setattr(agents_module, "utc_now", lambda: second_now)
    await service3.agent_coordinator.reconcile_sessions(now=second_now)
    second_state = await service3.agent_coordinator.message_state(agent_id)
    second_journal = await AgentStore(repo3.path).message_journal(agent_id)
    second_alerts = [
        item for item in second_journal if item["sender_agent_id"] == "system-session"
    ]

    assert second_state["alert_pending"] is True
    assert len(second_alerts) == 1
    assert second_alerts[0]["message_hash"] == first_alerts[0]["message_hash"]
    await terminal3.stop()


@pytest.mark.asyncio
async def test_startup_idle_expiry_releases_persisted_task_claim(tmp_path):
    from terminal_mcp.storage.tasks import TaskStore

    policy = AgentPolicy(idle_ttl_seconds=300, max_session_seconds=1500)
    repo, terminal, service = await runtime(tmp_path, policy=policy)
    plan = {
        "task_summary": "restart claim",
        "intent": "hold claim until downtime expiry",
        "details": ["claim", "restart"],
        "work_scope": ["repo:synthetic"],
    }
    proposal = await service.agent_start(**plan)
    started = await service.agent_start(
        agent_id=proposal["proposed_agent_id"],
        **plan,
    )
    agent_id = started["self"]["agent_id"]
    created = await service.task(
        agent_id,
        action="create",
        namespace="restart",
        task_id="LEASE",
        title="restart lease",
        isolation_hint="none",
    )
    assert created["ok"] is True
    claimed = await service.task(
        agent_id,
        action="claim",
        namespace="restart",
        task_id="LEASE",
        claim_intent="persist until session expiry",
    )
    assert claimed["ok"] is True

    now = parse_utc(utc_text(utc_now()))
    old_activity = utc_text(now - timedelta(seconds=301))
    future_expiry = utc_text(now + timedelta(seconds=1000))
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE agent_sessions SET last_activity_at=?,global_expires_at=? "
            "WHERE agent_id=?",
            (old_activity, future_expiry, agent_id),
        )
        db.commit()
    await terminal.stop()

    repo2, terminal2, service2 = await runtime(tmp_path, policy=policy)
    result = await service2.agent_coordinator.reconcile_sessions(now=now)
    stored = await AgentStore(repo2.path).get_session(agent_id)
    claims = await TaskStore(repo2.path).claims_for_agent(agent_id, active_only=True)

    assert result["expired"] == 1
    assert stored["state"] == "forced"
    assert stored["end_reason"] == "idle_timeout"
    assert claims == []
    await terminal2.stop()


@pytest.mark.asyncio
async def test_upgrade_repairs_legacy_terminal_history_before_fleet_sync(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)
    started = "2026-09-19T01:10:59.207Z"
    last_activity = "2026-09-19T01:11:46.534Z"
    agent_id = "Legacy-01234567"
    await store.create_session(
        agent_id,
        "legacy task",
        "legacy intent",
        ["repo:synthetic"],
        ["legacy step"],
        1,
        last_activity,
        registered_at=started,
        global_expires_at="2026-09-19T01:35:59.207Z",
    )
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE agent_sessions SET state='finished',ended_at=NULL,end_reason=NULL "
            "WHERE agent_id=?",
            (agent_id,),
        )
        db.commit()

    result = await service.agent_coordinator.reconcile_sessions()
    repaired = await store.get_session(agent_id)

    assert result == {"examined": 0, "backfilled": 0, "expired": 0}
    assert repaired["state"] == "finished"
    assert repaired["ended_at"] == last_activity
    assert repaired["end_reason"] == "explicit"

    await service.agent_coordinator.reconcile_sessions()
    preserved = await store.get_session(agent_id)
    assert preserved["ended_at"] == last_activity
    assert preserved["end_reason"] == "explicit"
    await terminal.stop()


@pytest.mark.asyncio
async def test_upgrade_repairs_unknown_legacy_forced_reason_without_resurrection(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)
    stamp = "2026-09-19T02:00:00.000Z"
    agent_id = "Legacy-89ABCDEF"
    await store.create_session(
        agent_id,
        "legacy forced task",
        "legacy forced intent",
        ["repo:synthetic"],
        ["legacy forced step"],
        1,
        stamp,
        registered_at="2026-09-19T01:59:00.000Z",
        global_expires_at="2026-09-19T02:24:00.000Z",
    )
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE agent_sessions SET state='forced',ended_at='',end_reason='' WHERE agent_id=?",
            (agent_id,),
        )
        db.commit()

    await service.agent_coordinator.reconcile_sessions()
    repaired = await store.get_session(agent_id)

    assert repaired["state"] == "forced"
    assert repaired["ended_at"] == stamp
    assert repaired["end_reason"] == "legacy_forced"
    await terminal.stop()
