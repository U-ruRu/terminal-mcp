import pytest

from terminal_mcp.core.orchestration import public_agent_name
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
    return await service.agent_start(
        task_summary=summary,
        intent=summary,
        details=[summary],
        work_scope=[f"test:{summary}"],
    )


@pytest.mark.asyncio
async def test_task_create_claim_warning_and_compact_listing(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        one = (await register(service, "one"))["self"]["agent_id"]
        two = (await register(service, "two"))["self"]["agent_id"]
        created = await service.task(
            one,
            action="create",
            namespace="project",
            task_id="REV-1",
            title="Review slice",
            lane="review",
            priority="P1",
            review_requirements=["A", "C"],
            resource_context={"repo": "/srv/repo"},
        )
        assert created["ok"] is True
        assert created["task"]["priority"] == "P1"
        assert "description" not in created["task"]

        first = await service.task(one, action="claim", namespace="project", task_id="REV-1")
        assert first["warnings"] == []
        second = await service.task(two, action="claim", namespace="project", task_id="REV-1")
        assert [item["code"] for item in second["warnings"]] == ["already_claimed"]
        assert {item["agent_name"] for item in second["task"]["claims"]} == {
            first["task"]["claims"][0]["agent_name"],
            second["task"]["claims"][1]["agent_name"],
        }

        fleet = await service.agents(one)
        assert fleet["self"]["managed_tasks"] == [
            {
                "namespace": "project",
                "task_id": "REV-1",
                "lane": "review",
                "priority": "P1",
                "state": "ready",
            }
        ]
        health = await service.health("none")
        assert health["workflow"]["by_lane"] == {"review": 1}
        assert health["workflow"]["active_claims"] == 2
        assert health["workflow"]["stale_claims"] == 0

        recovery = await service.recovery("printf task-event", agent_id=one)
        assert recovery["ok"] is True

        listing = await service.tasks(namespace="project")
        assert listing["ok"] is True
        assert listing["summary"]["by_lane"] == {"review": 1}
        assert listing["tasks"][0]["task_id"] == "REV-1"
        assert "resource_context" not in listing["tasks"][0]
        detailed = await service.tasks(namespace="project", task_id="REV-1", show_details=True)
        assert detailed["task"]["resource_context"] == {"repo": "/srv/repo"}
        command_event = next(
            item for item in detailed["task"]["events"] if item["event_type"] == "command"
        )
        assert command_event["payload"]["command_hash"] == recovery["cmd_hash"]
        assert command_event["payload"]["command_type"] == "recovery"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_addressed_message_routes_to_live_claimants_and_persists(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "sender"))["self"]["agent_id"]
        worker = (await register(service, "worker"))["self"]["agent_id"]
        await service.task(
            sender,
            action="create",
            namespace="project",
            task_id="REV-2",
            title="Task mailbox",
        )
        claimed = await service.task(worker, action="claim", namespace="project", task_id="REV-2")
        worker_name = claimed["task"]["claims"][0]["agent_name"]

        sent = await service.message(
            sender,
            text="Please review the candidate contract",
            namespace="project",
            task_id="REV-2",
            require_reply=True,
        )
        assert sent["ok"] is True
        assert sent["namespace"] == "project"
        assert sent["task_id"] == "REV-2"
        assert sent["delivered_to"] == [worker_name]

        surfaced = await service.read("deadbeef", agent_id=worker)
        assert any(
            "task project/REV-2" in line and sent["message_hash"] in line
            for line in surfaced["pending_messages"]
        )
        ack = await service.message(worker, message_hash=sent["message_hash"])
        assert ack["read_by"] == [worker_name]
        assert ack["namespace"] == "project"
        assert ack["task_id"] == "REV-2"
        reply = await service.message(
            worker, message_hash=sent["message_hash"], text="Contract reviewed"
        )
        assert reply["namespace"] == "project"
        assert reply["task_id"] == "REV-2"

        detail = await service.tasks(namespace="project", task_id="REV-2", show_details=True)
        messages = [item for item in detail["task"]["events"] if item["event_type"] == "message"]
        by_hash = {item["payload"]["message_hash"]: item for item in messages}
        assert by_hash[sent["message_hash"]]["payload"]["text"] == (
            "Please review the candidate contract"
        )
        assert by_hash[reply["reply_message_hash"]]["payload"]["reply_to"] == sent["message_hash"]
        assert by_hash[reply["reply_message_hash"]]["payload"]["text"] == "Contract reviewed"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_addressed_message_is_durable_without_live_claimants(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "sender"))["self"]["agent_id"]
        await service.task(
            sender,
            action="create",
            namespace="server",
            task_id="TASK-1",
            title="Unclaimed task",
        )
        sent = await service.message(
            sender,
            text="Durable note for the next claimant",
            namespace="server",
            task_id="TASK-1",
        )
        assert sent["ok"] is True
        assert sent["delivered_to"] == []
        detail = await service.tasks(namespace="server", task_id="TASK-1", show_details=True)
        assert any(
            item["event_type"] == "message"
            and item["payload"]["message_hash"] == sent["message_hash"]
            for item in detail["task"]["events"]
        )
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_agent_finish_releases_claim_with_durable_event(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "finisher"))["self"]["agent_id"]
        await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="FINISH-1",
            title="Finish releases claim",
        )
        await service.task(agent_id, action="claim", namespace="project", task_id="FINISH-1")

        finished = await service.agent_finish(agent_id)
        assert finished["ok"] is True
        assert finished["finished"] is True

        detail = await service.tasks(namespace="project", task_id="FINISH-1", show_details=True)
        assert detail["task"]["claims"] == []
        release = next(
            event for event in detail["task"]["events"] if event["event_type"] == "claim_released"
        )
        assert release["payload"]["reason"] == "agent_finish"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_unbound_candidate_review_does_not_satisfy_required_dimension(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = (await register(service, "owner-candidate"))["self"]["agent_id"]
        reviewer = (await register(service, "reviewer-candidate"))["self"]["agent_id"]
        await service.task(
            owner,
            action="create",
            namespace="project",
            task_id="CANDIDATE-1",
            title="Candidate binding",
            review_requirements=["A"],
        )
        reviewed = await service.task(
            reviewer,
            action="review",
            namespace="project",
            task_id="CANDIDATE-1",
            candidate_ref="unrelated-sha",
            dimensions=["A"],
            verdict="NON_BLOCKING",
        )
        assert reviewed["ok"] is True

        done = await service.task(owner, action="done", namespace="project", task_id="CANDIDATE-1")
        assert done["ok"] is True
        assert done["task"]["state"] == "done"
        assert [warning["code"] for warning in done["warnings"]] == ["review_incomplete"]
        assert done["warnings"][0]["context"]["dimensions"] == ["A"]
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_invalid_dependencies_do_not_partially_create_or_update_task(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "dependency-validator"))["self"]["agent_id"]
        created = await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="BAD-DEPS",
            title="Bad dependencies",
            dependencies=[{}],
        )
        assert created["ok"] is False
        assert "task_id: required" in created["error"]
        missing = await service.tasks(namespace="project", task_id="BAD-DEPS", show_details=True)
        assert missing["ok"] is False
        assert missing["task"] is None

        await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="GOOD",
            title="Original title",
        )
        updated = await service.task(
            agent_id,
            action="update",
            namespace="project",
            task_id="GOOD",
            title="Should not apply",
            dependencies=[{}],
        )
        assert updated["ok"] is False
        current = await service.tasks(namespace="project", task_id="GOOD", show_details=True)
        assert current["task"]["title"] == "Original title"

        duplicate = [
            {"namespace": "project", "task_id": "DEP"},
            {"namespace": "project", "task_id": "DEP"},
        ]
        partial_create = await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="ATOMIC-CREATE",
            title="Must roll back",
            dependencies=duplicate,
        )
        assert partial_create["ok"] is False
        missing_atomic = await service.tasks(
            namespace="project", task_id="ATOMIC-CREATE", show_details=True
        )
        assert missing_atomic["task"] is None

        before = await service.tasks(namespace="project", task_id="GOOD", show_details=True)
        before_revision = before["task"]["revision"]
        partial_update = await service.task(
            agent_id,
            action="update",
            namespace="project",
            task_id="GOOD",
            title="Must also roll back",
            dependencies=duplicate,
        )
        assert partial_update["ok"] is False
        after = await service.tasks(namespace="project", task_id="GOOD", show_details=True)
        assert after["task"]["title"] == "Original title"
        assert after["task"]["revision"] == before_revision
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_release_without_claim_does_not_create_false_release_event(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "release-idempotent"))["self"]["agent_id"]
        await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="RELEASE-1",
            title="Idempotent release",
        )
        released = await service.task(
            agent_id, action="release", namespace="project", task_id="RELEASE-1"
        )
        assert released["ok"] is True
        detail = await service.tasks(namespace="project", task_id="RELEASE-1", show_details=True)
        assert all(event["event_type"] != "claim_released" for event in detail["task"]["events"])
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_events_preserve_checkpoint_and_review_evidence_history(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = (await register(service, "history-owner"))["self"]["agent_id"]
        reviewer = (await register(service, "history-reviewer"))["self"]["agent_id"]
        await service.task(
            owner,
            action="create",
            namespace="project",
            task_id="HISTORY-1",
            title="Preserve history",
            candidate_ref="sha1",
            review_requirements=["A"],
        )
        await service.task(
            owner,
            action="checkpoint",
            namespace="project",
            task_id="HISTORY-1",
            checkpoint={"step": "one"},
        )
        await service.task(
            owner,
            action="checkpoint",
            namespace="project",
            task_id="HISTORY-1",
            checkpoint={"step": "two"},
        )
        await service.task(
            reviewer,
            action="review",
            namespace="project",
            task_id="HISTORY-1",
            candidate_ref="sha1",
            dimensions=["A"],
            verdict="NON_BLOCKING",
            evidence={"tests": 1},
        )
        await service.task(
            reviewer,
            action="review",
            namespace="project",
            task_id="HISTORY-1",
            candidate_ref="sha1",
            dimensions=["A"],
            verdict="BLOCKING",
            evidence={"tests": 2},
        )

        detail = await service.tasks(namespace="project", task_id="HISTORY-1", show_details=True)
        assert all("agent_id" not in event for event in detail["task"]["events"])
        visible_names = {
            event.get("agent_name") for event in detail["task"]["events"] if event.get("agent_name")
        }
        assert visible_names <= {public_agent_name(owner), public_agent_name(reviewer)}
        checkpoints = [
            event["payload"]["checkpoint"]
            for event in reversed(detail["task"]["events"])
            if event["event_type"] == "checkpoint"
        ]
        assert checkpoints == [{"step": "one"}, {"step": "two"}]
        reviews = [
            event["payload"]
            for event in reversed(detail["task"]["events"])
            if event["event_type"] == "review"
        ]
        assert [event["evidence"] for event in reviews] == [{"tests": 1}, {"tests": 2}]
        assert [event["verdict"] for event in reviews] == ["NON_BLOCKING", "BLOCKING"]
        assert detail["task"]["reviews"][0]["evidence"] == {"tests": 2}
    finally:
        await terminal.stop()
