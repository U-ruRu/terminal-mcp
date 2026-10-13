"""Agent service, lifecycle and durable-store wiring; process execution is isolated."""

import asyncio
import sqlite3
from datetime import timedelta

import pytest
from agent_test_helpers import register, runtime

import terminal_mcp.core.agents as agents_module
from terminal_mcp.core.orchestration import public_agent_name, utc_now, utc_text
from terminal_mcp.storage.agents import AgentStore


@pytest.mark.asyncio
async def test_public_name_collision_retry_preserves_task_history(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)
    now = utc_text()
    await store.create_session("Alpha-1111", "one", "first", ["repo:storage"], ["first"], 1, now)

    ids = iter(["Alpha-99999999", "Bravo-22222222"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))
    created = await register(service, "Implement registry", "Inspect schema", ["repo:storage"])
    assert created["self"]["agent_id"] == "Bravo-22222222"
    updated = await service.coordinate("Bravo-22222222", step=1, intent="Edit schema")
    assert updated["agent_name"] == "Bravo"
    assert updated["intent"] == "Edit schema"
    assert updated["step"] == 1
    overview = await service.agents("Bravo-22222222")
    assert overview["self"]["work_scope"] == ["repo:storage"]
    with sqlite3.connect(repo.path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM agent_task_events WHERE agent_id='Bravo-22222222'"
        ).fetchone()[0]
    assert count == 2
    await terminal.stop()


@pytest.mark.asyncio
async def test_agents_projects_logical_liveness_separately_from_scoped_intent_freshness(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    service.agent_coordinator.local_instance_id = "server-a"
    started = await register(service, "Scoped intent", "local fresh", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    store = AgentStore(repo.path)

    now = utc_now()
    remote_updated = utc_text(now - timedelta(seconds=400))
    current_activity = utc_text(now - timedelta(seconds=1))
    assert await store.apply_origin_session_update(
        agent_id,
        "server-b",
        current_activity,
        intent="remote stale",
        step=1,
        intent_updated_at=remote_updated,
        local_instance_id="server-a",
    )

    observed = await service.agents(agent_id, target=public_agent_name(agent_id))
    session = observed["sessions"][0]
    assert session["logical_session_status"] == "active"
    assert session["logical_idle_seconds"] < 10
    assert session["local_intent_status"] == "fresh"
    projected_scopes = [
        (item["instance_id"], item["status"], item["local"]) for item in session["intent_scopes"]
    ]
    assert projected_scopes == [
        ("server-a", "fresh", True),
        ("server-b", "stale", False),
    ]
    assert session["intent"] == "local fresh"
    await terminal.stop()


@pytest.mark.asyncio
async def test_active_filtering_and_finish(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "Finish", "Finish session", ["repo:tests"])
    agent_id = started["self"]["agent_id"]
    old = utc_text(utc_now() - timedelta(seconds=600))
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?", (old, agent_id))
        db.commit()
    service.agent_coordinator.ttl_seconds = 300
    expired = await service.agents(agent_id)
    assert expired["registration_required"] is True

    fresh = await register(service, "Finish", "Finish session", ["repo:tests"])
    fid = fresh["self"]["agent_id"]
    finished = await service.agent_finish(fid)
    assert finished["ok"] is True
    assert finished["agent_name"] == public_agent_name(fid)
    assert finished["finished"] is True
    assert finished.get("pending_messages", []) == []
    assert (await service.agents(fid))["registration_required"] is True
    await terminal.stop()


@pytest.mark.asyncio
async def test_active_overview_enforces_eight_agent_limit(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    caller = await register(service, "Caller", "Observe peers", ["repo:tests"])
    caller_id = caller["self"]["agent_id"]
    for index in range(10):
        await register(service, f"Peer {index}", f"Task {index}", [f"repo:peer/{index}"])
    overview = await service.agents(caller_id)
    assert len(overview["active"]) == 8
    assert overview["additional_active_agents"] == 2
    await terminal.stop()


@pytest.mark.asyncio
async def test_public_name_is_reserved_for_180_seconds_after_finished_session(
    tmp_path, monkeypatch
):
    repo, terminal, service = await runtime(tmp_path)
    ids = iter(["India-11111111", "India-22222222", "Juliett-33333333", "India-44444444"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))

    first = await register(service, "First", "Work", ["repo:first"])
    assert first["self"]["agent_id"] == "India-11111111"
    assert (await service.agent_finish("India-11111111"))["finished"] is True

    proposed = await service.agent_start(
        task_summary="Second",
        intent="Work",
        details=["Work"],
        work_scope=["repo:second"],
    )
    assert proposed["proposed_agent_id"] == "Juliett-33333333"

    old = utc_text(utc_now() - timedelta(seconds=181))
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE agent_sessions SET ended_at=? WHERE agent_id=?",
            (old, "India-11111111"),
        )
        db.commit()

    reusable = await service.agent_start(
        task_summary="Third",
        intent="Work",
        details=["Work"],
        work_scope=["repo:third"],
    )
    assert reusable["proposed_agent_id"] == "India-44444444"
    await terminal.stop()


@pytest.mark.asyncio
async def test_agent_plan_coordinate_and_agent_start_update(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    missing_plan = await service.agent_start(
        task_summary="Missing plan",
        intent="Cannot register",
    )
    assert missing_plan["ok"] is False
    assert "details" in missing_plan["error"]

    started = await register(
        service,
        "Plan test",
        "Inspect current state",
        details=["Inspect current state", "Patch implementation", "Run tests"],
    )
    agent_id = started["self"]["agent_id"]
    no_op = await service.agent_start(agent_id=agent_id)
    assert no_op["ok"] is True
    assert no_op["self"]["task_summary"] == "Plan test"
    assert no_op["self"]["details"] == [
        "Inspect current state",
        "Patch implementation",
        "Run tests",
    ]
    assert started["self"]["work_scope"] == []
    assert started["self"]["details"] == [
        "Inspect current state",
        "Patch implementation",
        "Run tests",
    ]
    assert started["self"]["current_step"] == 1

    inspected = await service.coordinate(agent_id, step=2)
    assert inspected["ok"] is True
    assert inspected["step"] == 2
    assert inspected["detail"] == "Patch implementation"
    assert inspected["intent"] == "Inspect current state"

    updated = await service.coordinate(agent_id, step=2, intent="Patching implementation")
    assert updated["ok"] is True
    assert updated["step"] == 2
    assert updated["intent"] == "Patching implementation"
    overview = await service.agents(agent_id)
    assert overview["self"]["current_step"] == 2

    intent_only = await service.agent_start(
        agent_id=agent_id,
        intent="Small plan-preserving correction",
    )
    assert intent_only["ok"] is True
    assert intent_only["self"]["details"] == [
        "Inspect current state",
        "Patch implementation",
        "Run tests",
    ]

    replanned = await service.agent_start(
        agent_id=agent_id,
        details=["Re-check design", "Finish implementation", "Regression"],
    )
    assert replanned["ok"] is True
    assert replanned["self"]["name"] == public_agent_name(agent_id)
    assert "agent_id" not in replanned["self"]
    assert replanned["self"]["details"][0] == "Re-check design"
    assert replanned["self"]["current_step"] == 2
    await terminal.stop()


@pytest.mark.asyncio
async def test_agent_start_projects_full_primary_context_only(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await service.context(
        "create", summary="Git workflow", content="Use the local Git wrapper.", primary=True
    )
    additional = await service.context(
        "create", summary="Docs", content="Read local docs.", primary=False
    )
    third = await service.context(
        "create", summary="Deploy", content="Use the documented deployment flow.", primary=True
    )

    started = await register(service, "Context projection", "Verify primary context")
    assert started["primary_context"] == [first["entry"], third["entry"]]
    assert all("content" in item for item in started["primary_context"])
    assert additional["entry"]["id"] not in {item["id"] for item in started["primary_context"]}

    await service.context("update", context_id=additional["entry"]["id"], primary=True)
    await service.context("delete", context_id=first["entry"]["id"])
    updated = await service.agent_start(
        agent_id=started["self"]["agent_id"], intent="Re-check primary context"
    )
    assert [item["id"] for item in updated["primary_context"]] == [
        additional["entry"]["id"],
        third["entry"]["id"],
    ]
    assert updated["primary_context"][0]["content"] == "Read local docs."

    listed = await service.context("list")
    assert all("content" not in item for item in listed["primary"] + listed["additional"])
    await terminal.stop()


@pytest.mark.asyncio
async def test_coordinate_show_details_exposes_other_agent_plan(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await register(
        service,
        "First",
        "Work on API",
        details=["Inspect API", "Patch API"],
    )
    second = await register(
        service,
        "Second",
        "Work on storage",
        details=["Inspect storage", "Patch storage", "Test storage"],
    )
    first_id = first["self"]["agent_id"]
    second_id = second["self"]["agent_id"]
    await service.coordinate(second_id, step=2, intent="Patching storage")

    result = await service.coordinate(first_id, show_details=True)
    second_name = public_agent_name(second_id)
    assert any(
        f"{second_name} step 2/3 — Patching storage" in line
        and "1. Inspect storage" in line
        and "3. Test storage" in line
        for line in result["other_details"]
    )
    await terminal.stop()


@pytest.mark.asyncio
async def test_broadcast_message_snapshots_recipients_and_receipts(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    sender = await register(service, "Sender", "Broadcast", details=["Broadcast"])
    one = await register(service, "One", "Listen", details=["Listen"])
    two = await register(service, "Two", "Listen", details=["Listen"])
    sender_id = sender["self"]["agent_id"]
    one_id = one["self"]["agent_id"]
    two_id = two["self"]["agent_id"]

    sent = await service.message(sender_id, text="Shared coordination note")
    assert sent["ok"] is True
    assert set(sent["delivered_to"]) == {
        public_agent_name(one_id),
        public_agent_name(two_id),
    }

    later = await register(service, "Later", "Joined later", details=["Joined later"])
    later_id = later["self"]["agent_id"]
    later_read = await service.read("deadbeef", agent_id=later_id)
    assert sent["message_hash"] not in str(later_read.get("pending_messages", []))

    sender_status = await service.message(sender_id, message_hash=sent["message_hash"])
    assert sender_status["ok"] is True
    assert sender_status["read_by"] == []

    ack = await service.message(one_id, message_hash=sent["message_hash"])
    assert ack["read_by"] == [public_agent_name(one_id)]
    sender_status = await service.message(sender_id, message_hash=sent["message_hash"])
    assert sender_status["read_by"] == [public_agent_name(one_id)]
    two_read = await service.read("deadbeef", agent_id=two_id)
    assert any(
        sent["message_hash"] in line and "→ all:" in line for line in two_read["pending_messages"]
    )

    invalid = await service.message(
        sender_id,
        text="Wrong target format",
        target=one_id,
    )
    assert invalid["ok"] is False
    assert "public agent name" in invalid["error"]
    await terminal.stop()


@pytest.mark.asyncio
async def test_pending_message_is_in_all_agent_bound_coordination_responses(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    sender = await register(service, "Sender", "Coordinate", details=["Coordinate"])
    receiver = await register(
        service,
        "Receiver",
        "Implement",
        details=["Implement", "Verify"],
    )
    sender_id = sender["self"]["agent_id"]
    receiver_id = receiver["self"]["agent_id"]
    sent = await service.message(
        sender_id,
        text="Please coordinate before changing storage.",
        target=public_agent_name(receiver_id),
    )
    message_hash = sent["message_hash"]

    coordinate = await service.coordinate(receiver_id)
    assert any(message_hash in line for line in coordinate["pending_messages"])

    overview = await service.agents(receiver_id)
    assert any(message_hash in line for line in overview["pending_messages"])

    cancelled = await service.cancel("deadbeef", agent_id=receiver_id)
    assert any(message_hash in line for line in cancelled["pending_messages"])

    updated_start = await service.agent_start(
        agent_id=receiver_id,
        intent="Still coordinating",
    )
    assert any(message_hash in line for line in updated_start["pending_messages"])

    own_message = await service.message(
        receiver_id,
        text="I am coordinating.",
        target=public_agent_name(sender_id),
    )
    assert any(message_hash in line for line in own_message["pending_messages"])

    finished = await service.agent_finish(receiver_id)
    assert any(message_hash in line for line in finished["pending_messages"])
    await terminal.stop()


@pytest.mark.asyncio
async def test_concurrent_admission_proposals_reserve_distinct_public_names(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)
    ids = iter(["Alpha-11111111", "Alpha-22222222", "Bravo-33333333"])
    monkeypatch.setattr(agents_module, "generate_agent_id", lambda: next(ids))
    original_create = store.create_proposal

    async def delayed_create(agent_id, now):
        await asyncio.sleep(0.02)
        await original_create(agent_id, now)

    service.agent_coordinator.store.create_proposal = delayed_create
    plan = {
        "task_summary": "Concurrent admission",
        "intent": "Reserve identity",
        "details": ["Reserve identity"],
        "work_scope": ["repo:synthetic"],
    }

    first, second = await asyncio.gather(
        service.agent_start(**plan),
        service.agent_start(**plan),
    )

    assert first["admission_required"] is True
    assert second["admission_required"] is True
    assert first["agent_name"] != second["agent_name"]
    assert {first["agent_name"], second["agent_name"]} == {"Alpha", "Bravo"}
    proposals = await store.recent_proposals(utc_text(utc_now() - timedelta(seconds=60)))
    assert {public_agent_name(item["agent_id"]) for item in proposals} >= {
        "Alpha",
        "Bravo",
    }
    await terminal.stop()


@pytest.mark.asyncio
async def test_two_step_admission_does_not_persist_until_confirmed(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    store = AgentStore(repo.path)

    proposal = await service.agent_start(
        task_summary="Synthetic task",
        intent="Inspect synthetic fixture",
        details=["Inspect synthetic fixture", "Run deterministic tests"],
        work_scope=["repo:synthetic"],
    )

    assert proposal["ok"] is False
    assert proposal["admission_required"] is True
    assert proposal["proposed_agent_id"].startswith(proposal["agent_name"] + "-")
    assert len(proposal["proposed_agent_id"].rsplit("-", 1)[1]) == 8
    assert await store.get_session(proposal["proposed_agent_id"]) is None

    confirmed = await service.agent_start(
        agent_id=proposal["proposed_agent_id"],
        task_summary="Synthetic task",
        intent="Inspect synthetic fixture",
        details=["Inspect synthetic fixture", "Run deterministic tests"],
        work_scope=["repo:synthetic"],
    )
    assert confirmed["ok"] is True
    assert confirmed["self"]["agent_id"] == proposal["proposed_agent_id"]
    assert (await store.get_session(proposal["proposed_agent_id"]))["state"] == "active"
    await terminal.stop()


@pytest.mark.asyncio
async def test_unknown_full_identity_fails_closed_without_trusted_fleet_record(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    agent_id = "Victor-01234567"

    rejected = await service.agent_start(
        agent_id=agent_id,
        task_summary="Synthetic resume",
        intent="Reuse cross-server identity",
        details=["Reuse cross-server identity"],
    )

    assert rejected["ok"] is False
    assert rejected["foreign_identity_unavailable"] is True
    assert rejected["retryable"] is True
    assert await AgentStore(repo.path).get_session(agent_id) is None
    await terminal.stop()


@pytest.mark.asyncio
async def test_ended_identity_returns_to_chat_and_cannot_be_recreated(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    started = await register(service, "Synthetic finish", "Finish safely")
    agent_id = started["self"]["agent_id"]

    assert (await service.agent_finish(agent_id))["finished"] is True
    ended = await service.agent_start(
        agent_id=agent_id,
        task_summary="Must not restart",
        intent="Must return to chat",
        details=["Must return to chat"],
    )

    assert ended["ok"] is False
    assert ended["return_to_chat"] is True
    assert ended["session_status"] == "finished"
    assert (await AgentStore(repo.path).get_session(agent_id))["state"] == "finished"
    await terminal.stop()


@pytest.mark.asyncio
async def test_unknown_identity_requires_40_bit_suffix(tmp_path):
    repo, terminal, service = await runtime(tmp_path)

    rejected = await service.agent_start(
        agent_id="Alpha-1234",
        task_summary="Legacy external id",
        intent="Should not admit",
        details=["Should not admit"],
    )
    assert rejected["ok"] is False
    assert rejected["admission_required"] is True
    assert "40-bit" in rejected["error"]
    await terminal.stop()
